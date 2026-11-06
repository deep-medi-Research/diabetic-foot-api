"""분석 프로그램을 실제로 띄워 이 저장소의 API 규칙을 확인합니다.

    python conformance/check_analyzer.py --analyzer "<명령>" --analyzer-dir <분석 프로그램 폴더> \\
        --variant <판> --recording <촬영 폴더>
    python conformance/check_analyzer.py --self-test

클라이언트 앱과 같은 인자로 분석 프로그램을 띄우고 준비 줄, preload, 읽지 못하는 요청 두 줄, 분석 요청 두 건(그림 미루기
켬과 끔), 표준 입력 닫기까지 진행합니다. 받은 줄과 결과 파일을 schema/의 스키마와 docs/analysis-api.md의 규칙으로
확인하고, 어긴 규칙마다 한 줄을 찍습니다. 하나라도 있으면 종료 코드 1입니다. 파이썬 3.9 이상의 표준 라이브러리만 씁니다.
"""
import argparse
import ast
import collections
import json
import os
import queue
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_check  # noqa: E402

API_VERSION = schema_check.api_version()
API_MAJOR = API_VERSION.split(".")[0]
STEP_TIMEOUT_S = 180
EXIT_TIMEOUT_S = 30
LAUNCH_ARGS = ["--idle-exit", "0", "--presegment-threads", "2", "--prestream"]
LAUNCH_ENV = {"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
UNKNOWN_VARIANT = "api-check-unknown-variant"
SIDES = ("left", "right")
REGIONS = ("toes", "fore", "mid", "heel")

# [dfr] 줄의 종류마다 내용 형식(json, text, none)과 json 내용의 스키마 파일입니다.
LINE_KINDS = {
    "ready": ("json", "ready.schema.json"),
    "preloaded": ("none", None),
    "preload-failed": ("text", None),
    "done": ("json", "done.schema.json"),
    "figures": ("json", "figures.schema.json"),
    "presegmented": ("json", "presegmented.schema.json"),
    "prestreamed": ("json", "prestreamed.schema.json"),
    "stamp-failed": ("text", None),
    "idle-exit": ("none", None),
}
SCHEMAS = {name: schema_check.load(name) for name in
           ["request.schema.json", "summary.schema.json", "variant.schema.json"]
           + [schema for _, schema in LINE_KINDS.values() if schema]}
PROGRESS = re.compile(r"^(\s*)\[(\d{1,6})/(\d{1,6})\]")

# 결과 npz의 배열 이름과 모양입니다. W는 요약의 n_windows이고, 같은 글자는 같은 길이입니다.
NUMERIC_DTYPES = {"<f8": 8, "<f4": 4, "<i8": 8, "<i4": 4}
PERFUSION_ARRAYS = dict(
    {
        "time_s": ("W",),
        "template_left": ("N",),
        "template_right": ("N",),
        "template_corr": ("W",),
        "n_light_ref": ("W",),
        "fps": (),
        "pi_left": ("W",),
        "pi_right": ("W",),
        "maps": (2, "W", "R", "C"),
        "map_valid": ("R", "C"),
        "outline": (3, "P"),
    },
    **{"pi_%s_%s" % (region, side): ("W",) for region in REGIONS for side in SIDES}
)
WAVEFORM_ARRAYS = {
    "fps": (),
    "ac_left": ("N",),
    "ac_right": ("N",),
    "pulse_left": ("N",),
    "pulse_right": ("N",),
    "pi_left": (),
    "pi_right": (),
}
DONE_FILES = ("summary.json", "perfusion.npz", "waveforms.npz")
FIGURE_FILES = ("segmentation.png", "template.png", "pi_map.png")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class Stop(Exception):
    """더 진행할 수 없는 위반입니다. 분석 프로그램을 끝내고 확인을 멈춥니다."""


def parse_line(text):
    """("dfr", 종류, 내용), ("progress", 하위 단계, n, m), ("other",) 중 하나입니다."""
    if text.startswith("[dfr] "):
        kind, _, payload = text[len("[dfr] "):].partition(" ")
        return ("dfr", kind, payload)
    match = PROGRESS.match(text)
    if match:
        return ("progress", bool(match.group(1)), int(match.group(2)), int(match.group(3)))
    return ("other",)


def strict_json(text):
    """줄의 JSON입니다. 표준 JSON이 아닌 NaN, Infinity는 받지 않습니다."""
    def reject(token):
        raise ValueError("%s는 표준 JSON이 아닙니다" % token)

    return json.loads(text, parse_constant=reject)


def parse_npy(data):
    """npy 바이트의 (dtype, 모양, fortran_order)입니다. 숫자 dtype이면 데이터 길이도 확인합니다."""
    if data[:6] != b"\x93NUMPY":
        raise ValueError("npy 머리가 아닙니다")
    if data[6] == 1:
        size, start = struct.unpack("<H", data[8:10])[0], 10
    elif data[6] in (2, 3):
        size, start = struct.unpack("<I", data[8:12])[0], 12
    else:
        raise ValueError("npy 머리 버전 %d을 받지 않습니다" % data[6])
    header = ast.literal_eval(data[start:start + size].decode("latin1"))
    descr, shape, fortran = header["descr"], tuple(header["shape"]), header["fortran_order"]
    if descr in NUMERIC_DTYPES:
        count = 1
        for n in shape:
            count *= n
        if len(data) - start - size != count * NUMERIC_DTYPES[descr]:
            raise ValueError("데이터 길이가 모양과 다릅니다")
    return descr, shape, fortran


def read_npz(path):
    """npz의 {배열 이름: (dtype, 모양, fortran_order)}입니다. 압축하거나 암호화한 항목이 있으면 ValueError입니다."""
    arrays = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if not info.filename.endswith(".npy"):
                continue
            if info.compress_type != zipfile.ZIP_STORED:
                raise ValueError("%s 항목이 압축되어 있습니다(np.savez_compressed)" % info.filename)
            if info.flag_bits & 0x1:
                raise ValueError("%s 항목이 암호화되어 있습니다" % info.filename)
            try:
                arrays[info.filename[:-4]] = parse_npy(archive.read(info))
            except ValueError as error:
                raise ValueError("%s: %s" % (info.filename, error)) from error
    return arrays


def match_shape(pattern, shape, bound):
    if len(pattern) != len(shape):
        return False
    for want, got in zip(pattern, shape):
        if isinstance(want, int):
            if want != got:
                return False
        elif bound.setdefault(want, got) != got:
            return False
    return True


def skip_sub_blocks(data, pos):
    while data[pos] != 0:
        pos += data[pos] + 1
    return pos + 1


def gif_frames(path):
    """GIF의 장 수입니다."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("GIF 머리가 아닙니다")
    pos = 13
    if data[10] & 0x80:
        pos += 3 * (2 << (data[10] & 7))
    frames = 0
    try:
        while True:
            block = data[pos]
            if block == 0x3B:
                return frames
            if block == 0x21:
                pos = skip_sub_blocks(data, pos + 2)
            elif block == 0x2C:
                frames += 1
                flags = data[pos + 9]
                pos += 10
                if flags & 0x80:
                    pos += 3 * (2 << (flags & 7))
                pos = skip_sub_blocks(data, pos + 1)
            else:
                raise ValueError("GIF 블록 0x%02x를 모릅니다" % block)
    except IndexError:
        raise ValueError("GIF가 끝 표시 없이 끝납니다") from None


def is_png(path):
    with open(path, "rb") as f:
        return f.read(len(PNG_SIGNATURE)) == PNG_SIGNATURE


def recording_inputs(rec_dir):
    """촬영 폴더의 `*_shot.json`으로 만든 분석 요청의 영상 키와 장 수 키입니다.

    매니페스트가 없으면 빈 dict이고, 분석 프로그램은 rec_dir의 left.avi, right.avi를 씁니다.
    """
    manifests = sorted(f for f in os.listdir(rec_dir) if f.endswith("_shot.json"))
    if not manifests:
        return {}
    with open(os.path.join(rec_dir, manifests[0]), encoding="utf-8") as f:
        files = json.load(f).get("files", {})
    inputs = {}
    for side in SIDES:
        inputs[side + "_video"] = os.path.join(rec_dir, files[side + "_video"])
        sidecar = files.get(side + "_sidecar")
        if sidecar and os.path.isfile(os.path.join(rec_dir, sidecar)):
            with open(os.path.join(rec_dir, sidecar), encoding="utf-8") as f:
                written = (json.load(f).get("frames") or {}).get("written")
            if isinstance(written, int) and not isinstance(written, bool):
                inputs[side + "_frames"] = written
    return inputs


class Analyzer:
    """띄운 분석 프로그램 하나입니다. 표준 출력과 표준 오류를 합쳐 줄 단위로 받습니다."""

    def __init__(self, argv, cwd, fail):
        self.fail = fail
        self.proc = subprocess.Popen(argv, cwd=cwd, env=dict(os.environ, **LAUNCH_ENV), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.lines = queue.Queue()
        self.tail = collections.deque(maxlen=20)
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        first = True
        for raw in iter(self.proc.stdout.readline, b""):
            if first and raw.startswith(b"\xef\xbb\xbf"):
                self.fail("표준 출력이 BOM으로 시작합니다")
            first = False
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("utf-8", "replace")
                self.fail("UTF-8이 아닌 줄이 있습니다: %s" % text.strip()[:80])
            if text.endswith("\n"):
                text = text[:-1]
            if text.endswith("\r"):
                text = text[:-1]
            self.lines.put(text)
        self.lines.put(None)

    def send(self, text):
        try:
            self.proc.stdin.write(text.encode("utf-8") + b"\n")
            self.proc.stdin.flush()
        except OSError:
            self.fail("표준 입력에 쓰지 못했습니다. 분석 프로그램이 끝났습니다")
            raise Stop() from None

    def next_line(self, timeout):
        """다음 줄입니다. 출력이 끝나면 None, timeout초 안에 없으면 queue.Empty입니다."""
        text = self.lines.get(timeout=max(0.0, timeout))
        if text is not None:
            self.tail.append(text)
        return text

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


class Checker:
    """분석 프로그램 하나를 클라이언트 앱처럼 몰아 가며 API 위반을 모읍니다."""

    def __init__(self, command, analyzer_dir, variant, recording, work):
        self.command = command
        self.analyzer_dir = analyzer_dir
        self.variant = variant
        self.recording = recording
        self.work = work
        self.violations = []
        self.notes = []
        self.context = "시작"
        self.ready = {}
        self.figures_expected = False

    def fail(self, message):
        entry = "[%s] %s" % (self.context, message)
        if not any(v.split("] ", 1)[1] == message for v in self.violations):
            self.violations.append(entry)

    def step(self, context):
        self.context = context
        print("- " + context, flush=True)

    def run(self):
        """확인한 위반 문장 목록입니다."""
        self.step("판 정보")
        self.check_variant_file()
        self.step("모르는 판")
        self.check_unknown_variant()
        analyzer = Analyzer(self.command + LAUNCH_ARGS + ["--variant", self.variant], self.analyzer_dir, self.fail)
        try:
            self.session(analyzer)
        except Stop:
            print("  분석 프로그램의 마지막 출력:")
            for line in analyzer.tail:
                print("  | " + line)
        finally:
            analyzer.stop()
        return self.violations

    def check_variant_file(self):
        path = os.path.join(self.analyzer_dir, "variants", self.variant, "VARIANT.json")
        if not os.path.isfile(path):
            self.fail("판 정보 파일이 없습니다: %s" % path)
            return
        try:
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
        except ValueError as error:
            self.fail("판 정보 파일을 JSON으로 읽지 못했습니다: %s" % error)
            return
        for error in SCHEMAS["variant.schema.json"].errors(info):
            self.fail("VARIANT.json: %s" % error)
        if isinstance(info, dict) and info.get("id") != self.variant:
            self.fail("VARIANT.json의 id %r가 폴더 이름 %r와 다릅니다" % (info.get("id"), self.variant))

    def check_unknown_variant(self):
        analyzer = Analyzer(self.command + LAUNCH_ARGS + ["--variant", UNKNOWN_VARIANT], self.analyzer_dir, self.fail)
        analyzer.proc.stdin.close()
        saw_ready = False
        try:
            while True:
                text = analyzer.next_line(STEP_TIMEOUT_S)
                if text is None:
                    break
                saw_ready = saw_ready or text.startswith("[dfr] ready")
        except queue.Empty:
            self.fail("모르는 판으로 띄우고 표준 입력을 닫았는데 %d초 안에 끝나지 않습니다" % STEP_TIMEOUT_S)
        analyzer.stop()
        if saw_ready:
            self.fail("모르는 판으로 띄웠는데 준비 줄을 찍었습니다")
        if analyzer.proc.returncode == 0:
            self.fail("모르는 판으로 띄웠는데 종료 코드가 0입니다")

    def session(self, analyzer):
        self.step("준비 줄")
        _, self.ready = self.expect(analyzer, {"ready"}, "띄운 뒤 %d초 안에 준비 줄이 없습니다" % STEP_TIMEOUT_S)
        self.check_identity(self.ready, "준비 줄")

        self.step("preload")
        self.send(analyzer, {"preload": True})
        self.expect(analyzer, {"preloaded", "preload-failed"}, "preload를 보낸 뒤 %d초 안에 답이 없습니다" % STEP_TIMEOUT_S)

        out1, out2 = os.path.join(self.work, "out1"), os.path.join(self.work, "out2")
        self.step("JSON이 아닌 요청 줄")
        analyzer.send("{not json")
        self.expect_refusal(analyzer)
        self.step("rec_dir이 없는 요청 줄")
        analyzer.send(json.dumps({"output": out1}, ensure_ascii=False))
        self.expect_refusal(analyzer)

        base = dict(recording_inputs(self.recording), rec_dir=self.recording, name="api_check", cuff_side="none",
                    template="v106w", pad_classifier=False)
        self.step("분석 요청 1 (그림 미루기, GIF, 커프 단계)")
        self.analysis(analyzer, dict(base, output=out1, classify=True, gif=True, defer_figures=True))
        self.step("분석 요청 2 (그림 함께, GIF 없음, 커프 단계 없음)")
        self.analysis(analyzer, dict(base, output=out2, classify=False, gif=False, defer_figures=False))

        self.step("표준 입력 닫기")
        analyzer.proc.stdin.close()
        try:
            while analyzer.next_line(EXIT_TIMEOUT_S) is not None:
                self.observe(analyzer.tail[-1])
            code = analyzer.proc.wait(EXIT_TIMEOUT_S)
        except (queue.Empty, subprocess.TimeoutExpired):
            self.fail("표준 입력을 닫은 뒤 %d초 안에 끝나지 않습니다" % EXIT_TIMEOUT_S)
            return
        if code != 0:
            self.fail("표준 입력을 닫은 뒤 종료 코드 %d로 끝났습니다. 0이어야 합니다" % code)

    def send(self, analyzer, request):
        for error in SCHEMAS["request.schema.json"].errors(request):
            raise AssertionError("확인 도구가 만든 요청이 스키마를 어깁니다: %s" % error)
        analyzer.send(json.dumps(request, ensure_ascii=False))

    def observe(self, text):
        """줄 하나를 해석하고 형식을 확인합니다. [dfr] 줄이면 (종류, JSON 내용), 아니면 (None, 진행 줄 여부)입니다."""
        line = parse_line(text)
        if line[0] == "progress":
            _, _, n, m = line
            if m == 0 or n > m:
                self.fail("진행 줄의 [n/m]은 m > 0, n <= m이어야 합니다: %s" % text)
            return None, True
        if line[0] != "dfr":
            return None, False
        _, kind, payload = line
        if kind not in LINE_KINDS:
            note = "모르는 [dfr] 줄 종류입니다(클라이언트 앱은 기록만 합니다): %s" % kind
            if note not in self.notes:
                self.notes.append(note)
            return kind, None
        form, schema = LINE_KINDS[kind]
        if kind == "figures" and not self.figures_expected:
            self.fail("그림 미루기를 요청하지 않았는데 figures 줄이 왔습니다")
        if form == "none" and payload:
            self.fail("%s 줄 뒤에는 내용이 없어야 합니다: %s" % (kind, text))
        if form != "json":
            return kind, payload
        try:
            value = strict_json(payload)
        except ValueError as error:
            self.fail("%s 줄의 JSON을 읽지 못했습니다(%s): %s" % (kind, error, text[:120]))
            return kind, None
        for error in SCHEMAS[schema].errors(value):
            self.fail("%s 줄: %s" % (kind, error))
        return kind, value

    def expect(self, analyzer, kinds, timeout_message, progress=False):
        """kinds 중 한 종류의 [dfr] 줄이 올 때까지 읽습니다. progress이면 진행 줄이 올 때마다 시간 제한을 새로 셉니다."""
        remaining = STEP_TIMEOUT_S
        while True:
            try:
                text = analyzer.next_line(remaining)
            except queue.Empty:
                self.fail(timeout_message)
                raise Stop() from None
            if text is None:
                self.fail("분석 프로그램이 끝났습니다(종료 코드 %s)" % analyzer.proc.wait())
                raise Stop()
            kind, value = self.observe(text)
            if progress and kind is None and value:
                remaining = STEP_TIMEOUT_S
            if kind in kinds:
                return kind, value

    def expect_refusal(self, analyzer):
        _, done = self.expect(analyzer, {"done"}, "%d초 안에 완료 줄이 없습니다" % STEP_TIMEOUT_S)
        if isinstance(done, dict) and done.get("ok") is not False:
            self.fail("읽지 못하는 요청에 ok가 false인 완료 줄로 답해야 합니다: %s" % json.dumps(done, ensure_ascii=False))

    def analysis(self, analyzer, request):
        self.send(analyzer, request)
        _, done = self.expect(analyzer, {"done"}, "진행 줄 없이 %d초가 지났습니다" % STEP_TIMEOUT_S, progress=True)
        if not isinstance(done, dict) or done.get("ok") is not True:
            self.fail("분석이 실패했습니다: %s" % (done or {}).get("error", "완료 줄을 읽지 못함"))
            return
        prefix = os.path.join(request["output"], request["name"] + "_")
        for suffix in DONE_FILES:
            if not os.path.isfile(prefix + suffix):
                self.fail("완료 줄을 받았을 때 %s%s가 없습니다" % (request["name"] + "_", suffix))
        summary = self.check_summary(prefix + "summary.json", request)
        if summary is not None and isinstance(summary.get("n_windows"), int):
            self.check_npz(prefix + "perfusion.npz", PERFUSION_ARRAYS, summary["n_windows"], series=True)
            self.check_npz(prefix + "waveforms.npz", WAVEFORM_ARRAYS, summary["n_windows"], series=False)
        if request["defer_figures"]:
            self.figures_expected = True
            _, figures = self.expect(analyzer, {"figures"}, "완료 줄 뒤 %d초 안에 figures 줄이 없습니다" % STEP_TIMEOUT_S)
            self.figures_expected = False
            if isinstance(figures, dict) and figures.get("ok") is False:
                self.fail("그림을 그리지 못했습니다: %s" % figures.get("error"))
            summary = self.check_summary(prefix + "summary.json", request)
        self.check_figures(prefix, request, summary)
        self.check_names(request)

    def check_identity(self, values, where):
        if not isinstance(values, dict):
            return
        version = values.get("api_version")
        if isinstance(version, str) and version.split(".")[0] != API_MAJOR:
            self.fail("%s의 api_version %s의 주 버전이 이 도구의 API %s와 다릅니다" % (where, version, API_VERSION))
        if "variant" in values and values["variant"] != self.variant:
            self.fail("%s의 variant %r가 띄운 판 %r와 다릅니다" % (where, values["variant"], self.variant))
        for key in ("api_version", "worker_version"):
            if key in values and key in self.ready and values[key] != self.ready[key]:
                self.fail("%s의 %s %r가 준비 줄의 값 %r와 다릅니다" % (where, key, values[key], self.ready[key]))

    def check_summary(self, path, request):
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            data = f.read()
        if any(b >= 0x80 for b in data):
            self.fail("요약에 ASCII 밖의 바이트가 있습니다. ASCII 밖 글자는 \\uXXXX로 씁니다")
        try:
            summary = json.loads(data.decode("utf-8", "replace"))
        except ValueError as error:
            self.fail("요약을 JSON으로 읽지 못했습니다: %s" % error)
            return None
        for error in SCHEMAS["summary.schema.json"].errors(summary):
            self.fail("요약: %s" % error)
        if not isinstance(summary, dict):
            return None
        self.check_identity(summary.get("dfr"), "요약 dfr")
        level = summary.get("cuff_level")
        wants_level = request.get("classify") and request.get("template", "v106w") == "v106w"
        if not wants_level and level is not None:
            self.fail("classify가 false이거나 template이 v106w가 아니면 cuff_level은 null이어야 합니다")
        if isinstance(level, dict) and level.get("quality_ok") is True \
                and level.get("level") != level.get("level_if_quality_ignored"):
            self.fail("quality_ok가 true이면 cuff_level.level과 level_if_quality_ignored가 같아야 합니다")
        return summary

    def check_npz(self, path, spec, n_windows, series):
        name = os.path.basename(path)
        if not os.path.isfile(path):
            return
        try:
            arrays = read_npz(path)
        except (ValueError, zipfile.BadZipFile, OSError) as error:
            self.fail("%s를 읽지 못했습니다: %s" % (name, error))
            return
        bound = {"W": n_windows}
        for key, pattern in spec.items():
            if key not in arrays:
                self.fail("%s에 배열 %s가 없습니다" % (name, key))
                continue
            descr, shape, fortran = arrays[key]
            if descr not in NUMERIC_DTYPES:
                self.fail("%s의 %s dtype %s는 %s 중 하나여야 합니다" % (name, key, descr, ", ".join(NUMERIC_DTYPES)))
            if fortran and len(shape) >= 2:
                self.fail("%s의 %s가 fortran_order입니다" % (name, key))
            if not match_shape(pattern, shape, bound):
                self.fail("%s의 %s 모양 %s가 %s와 맞지 않습니다(W는 n_windows %d)"
                          % (name, key, shape, pattern, n_windows))
        if bound.get("P", 2) < 2:
            self.fail("%s의 outline 점 수가 2보다 적습니다" % name)
        if series:
            for key, (descr, shape, _) in sorted(arrays.items()):
                if descr in NUMERIC_DTYPES and key.endswith(("_left", "_right")) and not key.startswith("template_") \
                        and shape != (n_windows,):
                    self.fail("%s의 %s는 이름이 _left, _right로 끝나므로 모양이 (%d,)이어야 합니다: %s"
                              % (name, key, n_windows, shape))

    def check_figures(self, prefix, request, summary):
        label = request["name"] + "_"
        for suffix in FIGURE_FILES:
            if not os.path.isfile(prefix + suffix):
                self.fail("%s%s가 없습니다" % (label, suffix))
            elif not is_png(prefix + suffix):
                self.fail("%s%s가 PNG가 아닙니다" % (label, suffix))
        gif = prefix + "perfusion.gif"
        if not request["gif"]:
            if os.path.exists(gif):
                self.fail("gif가 false인데 %sperfusion.gif가 있습니다" % label)
        elif not os.path.isfile(gif):
            self.fail("%sperfusion.gif가 없습니다" % label)
        elif summary is not None:
            try:
                frames = gif_frames(gif)
            except (ValueError, OSError) as error:
                self.fail("%sperfusion.gif를 읽지 못했습니다: %s" % (label, error))
            else:
                if frames != summary.get("n_windows"):
                    self.fail("GIF 장 수 %d가 n_windows %s와 다릅니다" % (frames, summary.get("n_windows")))
        self.check_report(prefix, label)

    def check_report(self, prefix, label):
        if os.path.isfile(prefix + "report.json"):
            try:
                with open(prefix + "report.json", encoding="utf-8") as f:
                    if not isinstance(json.load(f), dict):
                        self.fail("%sreport.json이 JSON 객체가 아닙니다" % label)
            except ValueError as error:
                self.fail("%sreport.json을 읽지 못했습니다: %s" % (label, error))
        if os.path.isfile(prefix + "report.npz"):
            try:
                read_npz(prefix + "report.npz")
            except (ValueError, zipfile.BadZipFile) as error:
                self.fail("%sreport.npz를 읽지 못했습니다: %s" % (label, error))
        folder, start = os.path.split(prefix + "report_")
        for entry in sorted(os.listdir(folder)):
            if entry.startswith(start) and entry.endswith(".png") and not is_png(os.path.join(folder, entry)):
                self.fail("%s가 PNG가 아닙니다" % entry)

    def check_names(self, request):
        for entry in sorted(os.listdir(request["output"])):
            if not entry.startswith(request["name"] + "_"):
                self.fail("결과 폴더에 이름이 %s_로 시작하지 않는 파일이 있습니다: %s" % (request["name"], entry))


def check(command, analyzer_dir, variant, recording, work=None):
    """확인을 돌리고 (위반 목록, 참고 목록)을 돌려줍니다. work가 없으면 임시 폴더를 쓰고 지웁니다."""
    temp = None
    if work is None:
        work = temp = tempfile.mkdtemp(prefix="api-check-")
    os.makedirs(work, exist_ok=True)
    try:
        checker = Checker(command, os.path.abspath(analyzer_dir), variant, os.path.abspath(recording),
                          os.path.abspath(work))
        return checker.run(), checker.notes
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)


def make_recording(root):
    """자체 시험용 촬영 폴더입니다. 가짜 분석 프로그램은 영상 내용을 읽지 않으므로 빈 영상 파일과 매니페스트만 둡니다."""
    rec = os.path.join(root, "시험_19550101", "A", "120000")
    os.makedirs(rec)
    files = {}
    for side in SIDES:
        files[side + "_video"] = "120000_%s.mkv" % side
        with open(os.path.join(rec, files[side + "_video"]), "wb") as f:
            f.write(b"\0")
    with open(os.path.join(rec, "120000_shot.json"), "w", encoding="utf-8") as f:
        json.dump({"files": files}, f, ensure_ascii=False)
    return rec


def client_check_test(fake, fake_dir):
    """가짜 분석 프로그램의 요청 확인입니다. API를 어긴 요청에는 어긴 키를 적은 실패 완료 줄과 0이 아닌 종료 코드가 나와야 합니다."""
    problems = []
    analyzer = Analyzer([sys.executable, fake] + LAUNCH_ARGS + ["--variant", "fake"], fake_dir, problems.append)
    try:
        lines = []
        analyzer.send(json.dumps({"output": "out"}))
        while True:
            text = analyzer.next_line(STEP_TIMEOUT_S)
            if text is None:
                break
            lines.append(text)
        code = analyzer.proc.wait(EXIT_TIMEOUT_S)
    finally:
        analyzer.stop()
    done = [strict_json(t[len("[dfr] done "):]) for t in lines if t.startswith("[dfr] done ")]
    if not done or done[-1].get("ok") is not False or "$.rec_dir" not in done[-1].get("error", ""):
        problems.append("rec_dir이 없는 요청에 $.rec_dir을 적은 실패 완료 줄이 없습니다: %s" % lines)
    if code == 0:
        problems.append("API를 어긴 요청을 받고 종료 코드 0으로 끝났습니다")
    return problems


def self_test():
    """가짜 분석 프로그램으로 이 도구를 확인합니다. 통과, 위반 찾기, 가짜의 클라이언트 확인 세 가지입니다."""
    fake_dir = os.path.join(schema_check.ROOT, "fake")
    fake = os.path.join(fake_dir, "fake_analyzer.py")
    command = [sys.executable, fake, "--no-client-check"]
    os.environ["FAKE_ANALYSIS_STAGE_MS"] = "20"
    problems = []
    work = tempfile.mkdtemp(prefix="api-self-test-")
    try:
        rec = make_recording(work)
        print("== 가짜 분석 프로그램으로 API %s 규칙 확인" % API_VERSION, flush=True)
        violations, _ = check(command, fake_dir, "fake", rec, os.path.join(work, "pass"))
        problems += ["가짜 분석 프로그램의 위반: " + v for v in violations]

        print("== worker_version을 뺀 가짜 분석 프로그램으로 위반 찾기 확인", flush=True)
        os.environ["FAKE_ANALYSIS_DROP"] = "worker_version"
        try:
            violations, _ = check(command, fake_dir, "fake", rec, os.path.join(work, "drop"))
        finally:
            del os.environ["FAKE_ANALYSIS_DROP"]
        if not any("ready 줄: $.worker_version" in v for v in violations):
            problems.append("준비 줄에 worker_version이 없는 것을 찾지 못했습니다")
        if not any("요약: $.dfr.worker_version" in v for v in violations):
            problems.append("요약 dfr에 worker_version이 없는 것을 찾지 못했습니다")
        problems += ["worker_version과 관계없는 위반: " + v for v in violations if "worker_version" not in v]

        print("== API를 어긴 요청에서 가짜 분석 프로그램의 종료 확인", flush=True)
        problems += client_check_test(fake, fake_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if problems:
        print("자체 시험 실패 %d건" % len(problems))
        for problem in problems:
            print("- " + problem)
        return 1
    print("자체 시험 통과 (API %s)" % API_VERSION)
    return 0


def split_command(text):
    """명령 문자열을 인자 목록으로 나눕니다. 첫 인자가 지금 폴더 기준으로 있는 파일이면 절대 경로로 바꿉니다."""
    parts = shlex.split(text, posix=(os.name != "nt"))
    parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    if parts and os.path.isfile(parts[0]):
        parts[0] = os.path.abspath(parts[0])
    return parts


def main(argv=None):
    parser = argparse.ArgumentParser(description="분석 프로그램을 띄워 API %s 규칙을 확인합니다" % API_VERSION)
    parser.add_argument("--analyzer", help='분석 프로그램 명령입니다. 예: "python entry.py"')
    parser.add_argument("--analyzer-dir", default=".", help="분석 프로그램 폴더입니다. 명령의 작업 폴더이고 variants/가 있습니다")
    parser.add_argument("--variant", help="확인할 판 이름입니다")
    parser.add_argument("--recording", help="분석할 촬영 폴더입니다. 이 폴더에는 아무것도 쓰지 않습니다")
    parser.add_argument("--work", help="결과 파일을 남길 폴더입니다. 주지 않으면 임시 폴더를 쓰고 지웁니다")
    parser.add_argument("--self-test", action="store_true", help="가짜 분석 프로그램으로 이 도구를 확인합니다")
    args = parser.parse_args(argv)
    # Windows에서 파이프로 받는 출력의 기본 인코딩은 코드 페이지라 한글 문장을 쓰지 못합니다.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if args.self_test:
        return self_test()
    if not (args.analyzer and args.variant and args.recording):
        parser.error("--analyzer, --variant, --recording을 함께 주거나 --self-test를 줍니다")
    violations, notes = check(split_command(args.analyzer), args.analyzer_dir, args.variant, args.recording, args.work)
    for note in notes:
        print("참고: " + note)
    if violations:
        print("API %s 위반 %d건" % (API_VERSION, len(violations)))
        for violation in violations:
            print("- " + violation)
        return 1
    print("API %s 확인 통과" % API_VERSION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
