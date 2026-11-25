import hashlib
import json
import os
import re
import shutil
import sys
import tkinter as tk
from codecs import BOM_UTF16_BE, BOM_UTF16_LE, BOM_UTF32_BE, BOM_UTF32_LE
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import partial
from itertools import count
from math import sin, tau
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic
from tkinter import filedialog, font, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from unicodedata import normalize


BASE_DIR = Path(__file__).resolve().parent
WORKDESK = BASE_DIR / "workdesk"
INPUT_DIR = WORKDESK / "input"
INPUT_OLD_DIR = WORKDESK / "input_old"
INPUT_PROBLEM_DIR = WORKDESK / "input_problem"
RESOLVED_PROBLEM_SUBDIR = "対応済み"
RECORDS_FILE = WORKDESK / "output" / "records.json"
MEMBERS_FILE = RECORDS_FILE.with_name("members.json")
PROBLEMS_FILE = WORKDESK / "output" / "problems.json"
STATUS_FILE = WORKDESK / "audit" / "agent_status.json"
MAX_ATTACHMENT_BYTES = 50_000_000


BACKGROUND = "#eeeeee"
SURFACE = "#ffffff"
INSET = "#f5f5f5"
BORDER = "#d9d9d9"
PRESSED = "#cccccc"
INK = "#202020"
MUTED = "#555555"
ACCENT = "#184aa5"
DATA_INK = "#193a72"
DATA_BACKGROUND = "#e4edff"
DATA_ALTERNATE = "#f0f5ff"
ALERT = "#962f38"
ALERT_BACKGROUND = "#fce7e7"
ALERT_ALTERNATE = "#fff2f2"
INDICATOR_BACKGROUND = INSET
ROBOT_FACE = (
    ".......A....",
    ".......#....",
    "...######...",
    "..#HHHHHM#..",
    ".#MSSSSSSM#.",
    "#DMSESSESMH#",
    "#DMSESSESMH#",
    ".#MSSGGSSM#.",
    ".#MHHMMMMM#.",
    "..#DDDDDD#..",
    "...######...",
)
ROBOT_COLORS = {
    # 稼働中、待機中の色
    ".": (INDICATOR_BACKGROUND, INDICATOR_BACKGROUND),
    "#": ("#143645", "#465c65"),
    "A": ("#efae35", "#c7b58d"),
    "H": ("#b9f0e6", "#cfddd9"),
    "M": ("#71c9b9", "#a1b4b4"),
    "D": ("#348d87", "#6b8589"),
    "S": ("#10303d", "#2f464f"),
    "E": ("#88ffe4", "#acd1c8"),
    "G": ("#43bba9", "#789b98"),
}
ROBOT_ICON_TOP = 2
STATUS_INTERVAL_MS = 500
STATUS_STALE_SECONDS = 20
DATA_INTERVAL_MS = 3000
INDICATOR_ANIMATION_INTERVAL_MS = 70
DEFAULT_FONT_SIZE = 10
FONT_SIZES = tuple(range(7, 17))
UNDATED = "日付なし"
PREVIEW_LIMIT = 64 * 1024
PREVIEW_BACKGROUND = "#292929"
TEXT_SUFFIXES = {".json", ".txt", ".csv", ".tsv", ".md", ".log", ".xml"}
NODE_INDICATORS = {
    "initial": ("pending", "入力確認"),
    "check_conflicts": ("summary", "勤務照合"),
    "add_record": ("processed", "勤怠登録"),
    "handle_error": ("problems", "エラー対応"),
    "add_problem": ("problems", "問題の記録"),
    "notify": ("problems", "通知案作成"),
    "return_to_initial": ("pending", "次の入力へ"),
    "stop_agent": ("pending", "終了処理"),
}


# [021][support] read_output_json
# {"機能概要":"指定ファイルを UTF-8 の JSON として読み込む。ファイルが存在しない場合や内容が空の場合は指定された既定値を返す。"}
def read_output_json(path, default):
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return default
    return json.loads(raw) if raw.strip() else default


# [022][support] lock_output
# {"機能概要":"保存先ディレクトリとロックファイルを用意し、OS に対応した排他ロックを処理区間の実行中に保持する。"}
@contextmanager
def lock_output(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


# [023][support] write_output_json
# {"機能概要":"データを同じディレクトリの一時ファイルへ JSON として書き込み、保存先を置き換える。処理後に残った一時ファイルを削除する。"}
def write_output_json(path, data):
    temp_path = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temp_path = Path(output.name)
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


# [025][support] is_work_period
# {"機能概要":"対象期間が YYYY-MM または YYYY-MM-DD の文字列であり、実在する年月または日付として解釈できるかを判定する。"}
def is_work_period(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}(?:-[0-9]{2})?", value) is None:
        return False
    try:
        date.fromisoformat(value + "-01" if len(value) == 7 else value)
    except ValueError:
        return False
    return True


# [026][support] worker_name_key
# {"機能概要":"氏名から空白文字を除去し、従業員の照合に使うキーを作成する。"}
def worker_name_key(name):
    return "".join(name.split())


# [027][support] is_employee_id
# {"機能概要":"従業員 ID が空でない文字列で、前後に余分な空白を含まないかを判定する。"}
def is_employee_id(value):
    return isinstance(value, str) and bool(value) and value == value.strip()


# [028][support] read_members
# {"機能概要":"従業員マスターを JSON から読み込み、形式と各項目を検証して返す。読み込みに失敗した場合は原因を示す例外を送出する。"}
def read_members(*, default=None):
    try:
        members = read_output_json(MEMBERS_FILE, default)
    except (OSError, ValueError) as error:
        raise ValueError("従業員マスター（members.json）を読み込めません。") from error
    validate_members(members)
    return members


# [029][support] validate_members
# {"機能概要":"従業員マスターが配列であること、各従業員の ID・氏名・正の整数の時給が有効であること、および ID が重複しないことを検証する。"}
def validate_members(members):
    if not isinstance(members, list):
        raise ValueError("従業員マスター（members.json）に従業員の JSON 配列を用意してください。")

    used_ids = set()
    for row, member in enumerate(members, start=1):
        if not isinstance(member, dict):
            raise ValueError(f"従業員マスターの {row} 件目の形式が不正です。")
        employee_id = member.get("employee_id")
        name = member.get("name")
        rate = member.get("hourly_rate_yen")
        if (not is_employee_id(employee_id) or not isinstance(name, str) or not name.strip()
                or type(rate) is not int or rate <= 0):
            raise ValueError(f"従業員マスターの {row} 件目の従業員ID、氏名、または時給が不正です。")
        if employee_id in used_ids:
            raise ValueError(f"従業員マスターの {row} 件目で従業員IDが重複しています。")
        used_ids.add(employee_id)


# [030][support] members_by_name
# {"機能概要":"空白を除去した氏名をキーとして、対応する従業員 ID と氏名の候補一覧を作成する。"}
def members_by_name(members):
    by_name = {}
    for member in members:
        employee_id, name = member["employee_id"], member["name"]
        by_name.setdefault(worker_name_key(name), []).append((employee_id, name.strip()))
    return by_name


# [032][support] legacy_record_targets
# {"機能概要":"従業員 ID を持たない旧形式の勤怠について、氏名から一意に特定できる従業員への紐付け候補と、紐付け後に重複する対象を調べる。"}
def legacy_record_targets(details, members):
    candidates = {}
    for employee_id, worker, period in details:
        if employee_id is None:
            matches = members.get(worker_name_key(worker), [])
            if len(matches) == 1:
                (matched_id, _), = matches
                candidates[employee_id, worker, period] = (matched_id, None, period)

    targets = Counter(candidates.values())
    collisions = {target for target, count in targets.items() if count > 1 or target in details}
    return candidates, collisions


# [033][support] read_record_details
# {"機能概要":"保存済み勤怠の形式・ID・対象期間・勤務時間・原本との関連付け・重複を検証する。旧形式も補完し、従業員と対象期間をキーにした辞書を返す。"}
def read_record_details():
    records = read_output_json(RECORDS_FILE, {})
    if not isinstance(records, dict):
        raise ValueError("records.json は勤怠記録を格納した JSON オブジェクトである必要があります。")

    details = {}
    legacy = {}
    used_ids = set()
    for key, record in records.items():
        if isinstance(record, str) and key.strip() and record.strip():
            legacy[key] = record
            continue
        if not isinstance(record, dict):
            raise ValueError("records.json の勤怠記録の形式が不正です。")

        record_id = record.get("id")
        employee_id = record.get("employee_id")
        worker = record.get("worker", key)
        period = record.get("work_period")
        hours = record.get("work_hours")
        sources = record.get("source_files", {})
        if (
            type(record_id) is not int or record_id < 1 or record_id in used_ids
            or (employee_id is not None and not is_employee_id(employee_id))
            or not isinstance(worker, str) or not worker.strip()
            or (period is not None and not is_work_period(period))
            or not isinstance(hours, str) or not hours.strip()
            or not isinstance(sources, dict)
            or any(not name or outcome not in ("added", "matched") for name, outcome in sources.items())
        ):
            raise ValueError("records.json の勤怠ID、従業員ID、氏名、対象期間、勤務時間、または証跡の形式が不正です。")
        identity = (employee_id, None if employee_id is not None else worker, period)
        if identity in details:
            raise ValueError("records.json に同じ従業員と対象期間の記録があります。")
        used_ids.add(record_id)
        details[identity] = {**record, "worker": worker, "work_period": period, "source_files": sources}

    for record_id, worker in enumerate(sorted(legacy), start=max(used_ids, default=0) + 1):
        identity = (None, worker, None)
        if identity in details:
            raise ValueError("records.json に同じ氏名と対象期間の記録があります。")
        details[identity] = {
            "id": record_id, "worker": worker, "work_period": None, "work_hours": legacy[worker], "source_files": {},
        }
    return details


# [036][support] input_version
# {"機能概要":"入力ファイルの識別情報・サイズ・更新時刻・属性変更時刻を取得し、処理中の変更検出に使う組を返す。"}
def input_version(path):
    stat = path.lstat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def read_agent_status():
    try:
        data = read_output_json(STATUS_FILE, None)
        if data is None:
            return "", "", "稼働状況の記録はまだありません。"
        if not isinstance(data, dict):
            raise ValueError
        phase, node = data["phase"], data["node"]
        updated = datetime.fromisoformat(data["updated_at"])
        if updated.tzinfo is None or not isinstance(node, str):
            raise ValueError
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        timestamp = updated.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        messages = {"active": "エージェント稼働中", "waiting": "入力待ち", "paused": "休止中",
                    "stopped": "エージェント停止", "interrupted": "エージェント中断", "failed": "エラーで終了"}
        message = messages[phase]
    except (OSError, ValueError, OverflowError, TypeError, KeyError):
        return "", "", "稼働状況の記録を読み込めません。"
    if phase in ("active", "waiting", "paused"):
        if age > STATUS_STALE_SECONDS or age < -STATUS_STALE_SECONDS:
            return "", "", f"稼働状況を確認できません。　最終更新 {timestamp}"
        if data.get("stop_requested"):
            message = "終了待ち　処理中の入力を完了して停止します。"
    return phase, node, f"{message}　最終更新 {timestamp}"


def fit_window_size(window, width, height):
    scale = window.winfo_fpixels("1i") / 96
    return (
        min(round(width * scale), window.winfo_screenwidth() * 9 // 10),
        min(round(height * scale), window.winfo_screenheight() * 9 // 10),
    )


def list_files(directory):
    try:
        return sorted(
            (path for path in directory.iterdir() if not path.is_symlink() and path.is_file()),
            key=lambda path: path.name.casefold(),
        )
    except FileNotFoundError:
        return []


@contextmanager
def stage_input_file():
    directory = INPUT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    # The agent scans top-level files, so it sees the file only after publication.
    with TemporaryDirectory(prefix=".incoming-", dir=directory, ignore_cleanup_errors=True) as staging:
        yield Path(staging) / "file"


def publish_input_file(staged, name):
    original = Path(name)
    for index in count():
        target = INPUT_DIR / (name if not index else f"{original.stem}_{index}{original.suffix}")
        try:
            if os.name == "nt":
                staged.rename(target)
            else:
                target.hardlink_to(staged)
        except FileExistsError:
            continue
        return target


def add_input_file(name, write_file):
    with stage_input_file() as staged:
        write_file(staged)
        return publish_input_file(staged, name)


def read_json_for_repair(path):
    if path.is_symlink():
        raise ValueError("一覧から元の保管ファイルを開き直してください。")
    version = input_version(path)
    data = path.read_bytes()
    if version != input_version(path):
        raise ValueError("ファイルが更新されました。一覧から開き直してください。")
    if data.startswith((BOM_UTF32_LE, BOM_UTF32_BE)):
        encoding = "utf-32"
    elif data.startswith((BOM_UTF16_LE, BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"
    return data.decode(encoding).replace("\r\n", "\n"), hashlib.sha256(data).digest()


def reject_json_constant(value):
    raise ValueError(f"JSONでは {value} を使用できません。")


def record_resubmission_request(archived_name):
    problems = read_problems()
    changed = False
    for problem in problems:
        names = problem.get("archive_names")
        archive = problem["files"] if names is None else names.values()
        if archived_name in archive and archived_name not in problem.get("resubmission_requests", []):
            problem.setdefault("resubmission_requests", []).append(archived_name)
            changed = True
    if changed:
        write_output_json(PROBLEMS_FILE, problems)


def resubmit_problem_json(path, original_digest, text):
    if path.parent != INPUT_PROBLEM_DIR or path.suffix.lower() != ".json":
        raise ValueError("再処理の対象は保管済みのJSONファイルです。")
    json.loads(text, parse_int=str, parse_float=str, parse_constant=reject_json_constant)
    data = text.encode("utf-8")
    if len(data) >= MAX_ATTACHMENT_BYTES:
        raise ValueError("再処理するJSONは50 MB未満にしてください。")

    with lock_output(PROBLEMS_FILE):
        if not path.is_file() or path.is_symlink():
            raise ValueError("元のファイルが移動しました。一覧から開き直してください。")
        with path.open("rb") as original:
            if hashlib.file_digest(original, "sha256").digest() != original_digest:
                raise ValueError("元のファイルが更新されました。一覧から開き直してください。")

        with stage_input_file() as staged:
            staged.write_bytes(data)
            # The problem stays visible until its archived file is removed.
            record_resubmission_request(path.name)
            target = publish_input_file(staged, path.name)

        # Keep the original available if writing or publishing the corrected file fails.
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            return target, f"再提出しましたが、元の保管ファイルを削除できませんでした。\n{error}"
        return target, None


def preview_file(path):
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return f"{path.name}\n原本の保存先  {path.parent.name}/{path.name}"
    try:
        with path.open("rb") as file:
            raw = file.read(PREVIEW_LIMIT + 1)
    except OSError as error:
        return f"{path.name} を読み込めませんでした（{error.strerror or error}）。"

    truncated = len(raw) > PREVIEW_LIMIT
    text = raw[:PREVIEW_LIMIT].decode("utf-8-sig", errors="replace")
    if path.suffix.lower() == ".json" and not truncated:
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    if truncated:
        text += "\n\n…先頭64 KBを表示しています。"
    return text if text.strip() else "ファイルは空です。"


def processed_file_detail(path, links):
    if links is None:
        summary = "反映先の勤怠記録を読み込めませんでした。"
    elif not links:
        summary = "このファイルに紐づく勤怠記録の証跡はありません。"
    else:
        labels = {"added": "新規登録", "matched": "登録済みと一致"}
        rows = [
            f"勤怠ID {record_id}｜従業員ID {employee_id or '未設定'}｜{worker}｜{period or UNDATED}｜{hours}｜{labels[outcome]}"
            for record_id, employee_id, worker, period, hours, outcome in sorted(links)
        ]
        summary = "反映先の勤怠記録\n" + "\n".join(rows)
    return f"{summary}\n\n原本\n{preview_file(path)}"


def problem_file_detail(path, detail):
    return f"{detail}\n\n原本\n{preview_file(path)}"


def work_minutes(value):
    value = normalize("NFKC", value).strip()
    duration = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*時間(?:\s*([0-9]{1,2})\s*分)?", value)
    if duration:
        hours, minutes = duration.groups()
        if minutes and (int(minutes) >= 60 or "." in hours):
            return None
        return Decimal(hours) * 60 + int(minutes or 0)
    minutes = re.fullmatch(r"([0-9]+)\s*分", value)
    if minutes:
        return Decimal(minutes[1])

    clock = r"([0-9]{1,2}):?([0-9]{2})"
    span = re.fullmatch(rf"{clock}\s*[-~〜–—−]\s*{clock}", value)
    if not span:
        return None
    start_hour, start_minute, end_hour, end_minute = map(int, span.groups())
    if start_hour >= 24 or end_hour > 24 or max(start_minute, end_minute) >= 60:
        return None
    if end_hour == 24 and end_minute != 0:
        return None
    elapsed = end_hour * 60 + end_minute - (start_hour * 60 + start_minute)
    return Decimal(elapsed if elapsed >= 0 else elapsed + 24 * 60)


def format_minutes(minutes):
    rounded = int(minutes.to_integral_value(rounding=ROUND_HALF_UP))
    hours, minutes = divmod(rounded, 60)
    return f"{hours:,}時間{minutes:02}分"


def monthly_totals(records, members):
    totals_by_month = {}
    incomplete = {}
    excluded = Counter()
    unmatched = Counter()
    monthly_reports = {}
    member_ids = {member["employee_id"] for member in members}
    targets, collisions = legacy_record_targets(records, members_by_name(members))
    for identity, record in records.items():
        worker = record["worker"]
        target = targets.get(identity, identity)
        employee_id, legacy_name, period = identity if target in collisions else target
        person = (employee_id, legacy_name)
        month = period[:7] if period else UNDATED
        if employee_id not in member_ids:
            unmatched[month] += 1
        totals = totals_by_month.setdefault(month, {})
        minutes = work_minutes(record["work_hours"])
        if minutes is None:
            excluded[month] += 1
            incomplete.setdefault(month, set()).add(person)
        elif period == month:
            monthly_reports[month, person] = (worker, minutes)
        else:
            _, previous_minutes = totals.get(person, (worker, Decimal(0)))
            totals[person] = (worker, previous_minutes + minutes)
    for (month, person), total in monthly_reports.items():
        # 月合計がある人は、同じ月の日別明細との二重集計を防ぐ。
        totals_by_month[month][person] = total
        incomplete.setdefault(month, set()).discard(person)
    for employee_id, _, period in collisions:
        month = period[:7] if period else UNDATED
        incomplete.setdefault(month, set()).add((employee_id, None))
    return totals_by_month, incomplete, excluded, unmatched


def payroll_rows(members, totals, incomplete, *, records_error):
    rows = []
    for member in members:
        employee_id, name = member["employee_id"], member["name"].strip()
        person = (employee_id, None)
        if records_error or person in incomplete:
            hours, salary = "集計不可", "—"
        else:
            _, minutes = totals.get(person, (name, Decimal(0)))
            yen = int((minutes * member["hourly_rate_yen"] / 60).to_integral_value(rounding=ROUND_HALF_UP))
            hours, salary = format_minutes(minutes), f"{yen:,}円"
        rows.append((employee_id, (employee_id, name, hours, salary), ""))
    return rows


def save_members(original, members):
    validate_members(members)
    # 勤怠登録と同じロックで、更新中に従業員IDの参照が増えないようにする。
    with lock_output(RECORDS_FILE):
        if read_members(default=[]) != original:
            raise ValueError("従業員リストが更新されています。編集画面を開き直してください。")
        records = read_record_details()
        removed_ids = {member["employee_id"] for member in original} - {member["employee_id"] for member in members}
        if any(employee_id in removed_ids for employee_id, _, _ in records):
            raise ValueError("勤怠記録がある従業員は、削除や従業員IDの変更ができません。")
        before, _ = legacy_record_targets(records, members_by_name(original))
        after, _ = legacy_record_targets(records, members_by_name(members))
        if any(after.get(key) != target for key, target in before.items()):
            raise ValueError("氏名で紐づく勤怠記録があります。既存の記録と従業員の対応を保つ必要があります。")
        write_output_json(MEMBERS_FILE, members)


def read_problems():
    problems = read_output_json(PROBLEMS_FILE, [])
    if not isinstance(problems, list):
        raise ValueError("problems.json はJSON配列である必要があります。")
    for index, problem in enumerate(problems, 1):
        if (
            not isinstance(problem, dict)
            or not isinstance(problem.get("description"), str)
            or not isinstance(problem.get("reason"), str)
            or not isinstance(problem.get("files"), list)
            or not all(isinstance(name, str) for name in problem["files"])
        ):
            raise ValueError(f"problems.json の{index}件目の形式が不正です。")
        if "archive_names" in problem:
            names = problem["archive_names"]
            if not isinstance(names, dict) or any(
                name not in problem["files"] or not isinstance(archived, str) or not archived
                for name, archived in names.items()
            ):
                raise ValueError(f"problems.json の{index}件目の保管ファイル名が不正です。")
        requested = problem.get("resubmission_requests", [])
        if not isinstance(requested, list) or not all(isinstance(name, str) for name in requested):
            raise ValueError(f"problems.json の{index}件目の再提出ファイル名が不正です。")
    return problems


class CameraFeed:
    device_lock = Lock()

    def __init__(self, cv, preview_size):
        self.cv = cv
        self.preview_size = preview_size
        self.frame = None
        self.error = None
        self.stop = Event()
        Thread(target=self.run, daemon=True).start()

    def run(self):
        cv = self.cv
        backends = (cv.CAP_MSMF, cv.CAP_DSHOW) if sys.platform == "win32" else (cv.CAP_ANY,)
        with self.device_lock:
            for backend in backends:
                if self.stop.is_set():
                    return
                camera = cv.VideoCapture()
                try:
                    if camera.open(0, backend):
                        camera.set(cv.CAP_PROP_FRAME_WIDTH, 1920)
                        camera.set(cv.CAP_PROP_FRAME_HEIGHT, 1080)
                        self.read_frames(camera)
                    if self.frame is not None:
                        break
                except cv.error:
                    if self.frame is not None:
                        break
                finally:
                    camera.release()

            if not self.stop.is_set():
                self.error = "カメラの映像を取得できません。接続とカメラへのアクセス許可を確認してください。"

    def read_frames(self, camera):
        cv = self.cv
        next_preview = 0
        last_frame = monotonic()
        while not self.stop.is_set():
            ok, frame = camera.read()
            now = monotonic()
            if not ok or frame is None:
                if now - last_frame >= 3:
                    return
                self.stop.wait(0.05)
                continue

            last_frame = now
            if now < next_preview:
                continue
            next_preview = now + 0.08
            height, width = frame.shape[:2]
            scale = min(self.preview_size[0] / width, self.preview_size[1] / height, 1)
            preview = frame
            if scale < 1:
                size = (max(1, int(width * scale)), max(1, int(height * scale)))
                preview = cv.resize(frame, size, interpolation=cv.INTER_AREA)
            rgb = cv.cvtColor(preview, cv.COLOR_BGR2RGB)
            height, width = rgb.shape[:2]
            ppm = f"P6\n{width} {height}\n255\n".encode("ascii") + rgb.tobytes()
            self.frame = (frame, ppm)


class CameraDialog(tk.Toplevel):
    def __init__(self, parent, cv, save_photo):
        super().__init__(parent)
        self.cv = cv
        self.save_photo = save_photo
        self.feed = None
        self.timer = None
        self.displayed_frame = None
        self.photo = None
        self.title("カメラで撮影")
        self.transient(parent)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())

        width = max(280, min(720, self.winfo_screenwidth() - 100))
        height = max(180, min(480, self.winfo_screenheight() - 220))
        self.preview_size = (width, height)
        content = ttk.Frame(self, padding=14)
        content.pack(fill="both", expand=True)
        viewport = tk.Frame(content, width=width, height=height, background=PREVIEW_BACKGROUND)
        viewport.pack()
        self.image_label = tk.Label(viewport, background=PREVIEW_BACKGROUND, borderwidth=0)
        self.image_label.place(relx=0.5, rely=0.5, anchor="center")
        self.prompt = ttk.Label(content, wraplength=width)
        self.prompt.pack(anchor="w", pady=(12, 9))

        buttons = ttk.Frame(content)
        buttons.pack(anchor="e")
        self.capture_button = ttk.Button(buttons, text="撮影", command=self.capture)
        self.capture_button.grid(row=0, column=0, padx=(0, 6))
        self.save_button = ttk.Button(buttons, text="はい", command=self.confirm_save)
        self.save_button.grid(row=0, column=0, padx=(0, 6))
        self.retry_button = ttk.Button(buttons, text="撮り直す", command=self.start_live_preview)
        self.retry_button.grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="キャンセル", command=self.destroy).grid(row=0, column=2)

        self.start_live_preview()
        self.grab_set()

    def start_live_preview(self):
        self.stop_live_preview()
        self.displayed_frame = None
        self.image_label.configure(image="")
        self.photo = None
        self.prompt.configure(text="カメラを起動しています…")
        self.save_button.grid_remove()
        self.retry_button.grid_remove()
        self.capture_button.grid()
        self.capture_button.state(["disabled"])
        self.feed = CameraFeed(self.cv, self.preview_size)
        self.update_live_preview()

    def update_live_preview(self):
        self.timer = None
        if self.feed.error is not None:
            self.prompt.configure(text=self.feed.error)
            self.capture_button.state(["disabled"])
            self.retry_button.configure(text="もう一度試す")
            self.retry_button.grid()
            return

        frame = self.feed.frame
        if frame is not None and frame is not self.displayed_frame:
            self.photo = tk.PhotoImage(master=self, data=frame[1], format="PPM")
            self.image_label.configure(image=self.photo)
            if self.displayed_frame is None:
                self.prompt.configure(text=" ")
                self.capture_button.state(["!disabled"])
            self.displayed_frame = frame
        self.timer = self.after(40, self.update_live_preview)

    def stop_live_preview(self):
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
        if self.feed is not None:
            self.feed.stop.set()
            self.feed = None

    def capture(self):
        if self.displayed_frame is None or self.feed is None or self.feed.error is not None:
            return
        self.stop_live_preview()
        self.prompt.configure(text="これを保存してよいですか？")
        self.capture_button.grid_remove()
        self.save_button.grid()
        self.retry_button.configure(text="撮り直す")
        self.retry_button.grid()

    def confirm_save(self):
        if self.displayed_frame is None or self.feed is not None:
            return
        try:
            ok, data = self.cv.imencode(".jpg", self.displayed_frame[0], [self.cv.IMWRITE_JPEG_QUALITY, 95])
            if not ok:
                raise ValueError("写真をJPEG形式に変換できませんでした。")
            self.save_photo(data.tobytes())
        except (OSError, ValueError, self.cv.error) as error:
            messagebox.showerror("写真を保存できませんでした", str(error), parent=self)
        else:
            self.destroy()

    def destroy(self):
        self.stop_live_preview()
        super().destroy()


class JsonRepairDialog(tk.Toplevel):
    def __init__(self, parent, path, source, original_digest, findings, on_submitted):
        super().__init__(parent)
        self.path = path
        self.original_digest = original_digest
        self.on_submitted = on_submitted
        self.title("勤怠データの修正")
        self.transient(parent)
        width, height = fit_window_size(self, 1000, 720)
        self.geometry(f"{width}x{height}")
        self.minsize(*fit_window_size(self, 480, 390))
        self.bind("<Escape>", lambda _event: self.destroy())

        content = ttk.Frame(self, padding=14)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(4, weight=1)
        ttk.Label(content, text=path.name).grid(row=0, column=0, sticky="w", pady=(0, 10))
        ttk.Label(content, text="指摘内容").grid(row=1, column=0, sticky="w")
        details = ScrolledText(
            content, height=5, width=1, wrap="word", font="TkDefaultFont", padx=7, pady=5,
            background=ALERT_BACKGROUND, foreground=ALERT,
        )
        details.grid(row=2, column=0, sticky="ew", pady=(3, 10))
        details.insert("1.0", findings)
        details.configure(state="disabled")
        ttk.Label(content, text="JSON").grid(row=3, column=0, sticky="w")
        self.editor = ScrolledText(
            content, width=1, height=1, wrap="char", undo=True, font="TkFixedFont", padx=7, pady=5,
            background=SURFACE, foreground=INK, selectbackground=ACCENT, selectforeground=SURFACE,
        )
        self.editor.grid(row=4, column=0, sticky="nsew", pady=(3, 8))
        self.editor.insert("1.0", source)
        self.editor.edit_reset()
        self.editor.mark_set("insert", "1.0")

        self.error = ttk.Label(content, foreground=ALERT, wraplength=430)
        self.error.grid(row=5, column=0, sticky="ew")
        buttons = ttk.Frame(content)
        buttons.grid(row=6, column=0, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="キャンセル", command=self.destroy).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="修正して再提出", command=self.submit).pack(side="left")
        self.grab_set()
        self.editor.focus_set()

    def submit(self):
        self.error.configure(text="")
        try:
            target, warning = resubmit_problem_json(self.path, self.original_digest, self.editor.get("1.0", "end-1c"))
        except json.JSONDecodeError as error:
            self.error.configure(text=f"JSONの構文エラー（{error.lineno}行 {error.colno}列） {error.msg}")
            self.editor.mark_set("insert", f"{error.lineno}.{error.colno - 1}")
            self.editor.see("insert")
            self.editor.focus_set()
        except RecursionError:
            self.error.configure(text="JSONの階層が深すぎます。")
        except (OSError, ValueError) as error:
            self.error.configure(text=str(error))
        else:
            if warning:
                messagebox.showwarning("勤怠データの再提出", warning, parent=self)
            self.destroy()
            self.on_submitted(target)


def create_robot_icon(master, *, waiting=False):
    palette = {pixel: inactive if waiting else active for pixel, (active, inactive) in ROBOT_COLORS.items()}
    rows = []
    for line in ROBOT_FACE:
        row = " ".join(palette[pixel] for pixel in line for _ in range(2))
        rows.extend([f"{{{row}}}"] * 2)
    icon = tk.PhotoImage(master=master, width=len(ROBOT_FACE[0]) * 2, height=len(rows))
    icon.put(" ".join(rows))
    return icon


def create_table(parent, columns, *, alert=False, horizontal=False):
    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(0, weight=1)
    column_ids = tuple(f"column{index}" for index in range(len(columns)))
    tree = ttk.Treeview(
        parent, columns=column_ids, show="headings", selectmode="browse", height=3,
        style="Problem.Treeview" if alert else "Treeview",
    )
    tree.grid(row=0, column=0, sticky="nsew")
    tree.tag_configure("alternate", background=ALERT_ALTERNATE if alert else DATA_ALTERNATE)
    scrollbar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
    scrollbar.grid(row=0, column=1, sticky="ns")
    tree.configure(yscrollcommand=scrollbar.set)
    if horizontal:
        scrollbar = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        scrollbar.grid(row=1, column=0, sticky="ew")
        tree.configure(xscrollcommand=scrollbar.set)
    for column, (heading, width, stretch) in zip(column_ids, columns):
        tree.heading(column, text=heading, anchor="w")
        tree.column(column, width=width, minwidth=45, stretch=stretch)
    return tree


class MemberEditor(tk.Toplevel):
    def __init__(self, parent, members, on_saved):
        super().__init__(parent)
        self.members = members
        self.on_saved = on_saved
        self.selected = None
        self.title("従業員リストの編集")
        self.transient(parent)
        width, height = fit_window_size(self, 660, 420)
        self.geometry(f"{width}x{height}")
        self.minsize(*fit_window_size(self, 520, 330))
        self.bind("<Escape>", lambda _event: self.destroy())

        content = ttk.Frame(self, padding=14)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        table = ttk.Frame(content)
        table.grid(row=0, column=0, sticky="nsew")
        self.tree = create_table(table, (("従業員ID", 120, False), ("氏名", 175, True), ("時給", 100, False)))
        self.tree.bind("<<TreeviewSelect>>", self.select_member)
        self.tree.column("column2", anchor="e")

        form = ttk.Frame(content)
        form.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.values = [tk.StringVar(master=self) for _ in range(3)]
        for index, (label, value) in enumerate(zip(("従業員ID", "氏名", "時給（円）"), self.values)):
            form.columnconfigure(index, weight=1)
            ttk.Label(form, text=label).grid(row=0, column=index, sticky="w", padx=(0, 6))
            entry = ttk.Entry(form, textvariable=value, width=12)
            entry.grid(row=1, column=index, sticky="ew", padx=(0, 6))
            if index == 0:
                self.id_entry = entry

        self.error = ttk.Label(content, foreground=ALERT, wraplength=440)
        self.error.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        buttons = ttk.Frame(content)
        buttons.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="新規追加", command=self.new_member).pack(side="left")
        self.delete_button = ttk.Button(buttons, text="削除", command=self.delete_member)
        self.delete_button.pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="閉じる", command=self.destroy).pack(side="right")
        self.save_button = ttk.Button(buttons, command=self.save_member)
        self.save_button.pack(side="right", padx=(0, 6))
        self.reload_table(0)
        self.grab_set()
        self.id_entry.focus_set()

    def reload_table(self, index):
        self.tree.delete(*self.tree.get_children())
        for row, member in enumerate(self.members):
            values = (member["employee_id"], member["name"], f"{member['hourly_rate_yen']:,}円")
            tags = ("alternate",) if row % 2 else ()
            self.tree.insert("", "end", iid=str(row), values=values, tags=tags)
        if self.members:
            row = str(min(index, len(self.members) - 1))
            self.tree.selection_set(row)
            self.tree.focus(row)
            self.tree.see(row)
            self.select_member()
        else:
            self.new_member()

    def select_member(self, _event=None):
        selection = self.tree.selection()
        if not selection or int(selection[0]) == self.selected:
            return
        self.selected = int(selection[0])
        member = self.members[self.selected]
        for value, key in zip(self.values, ("employee_id", "name", "hourly_rate_yen")):
            value.set(member[key])
        self.error.configure(text="")
        self.save_button.configure(text="変更を保存")
        self.delete_button.state(["!disabled"])

    def new_member(self):
        self.selected = None
        self.tree.selection_remove(*self.tree.selection())
        for value in self.values:
            value.set("")
        self.error.configure(text="")
        self.save_button.configure(text="追加して保存")
        self.delete_button.state(["disabled"])
        self.id_entry.focus_set()

    def save_member(self):
        employee_id, name, rate = (value.get().strip() for value in self.values)
        rate = normalize("NFKC", rate)
        if not employee_id or not name:
            self.error.configure(text="従業員IDと氏名を入力してください。")
            return
        if not re.fullmatch(r"[0-9]+", rate) or not rate.strip("0"):
            self.error.configure(text="時給は1円以上の整数で入力してください。")
            return
        if any(member["employee_id"] == employee_id for row, member in enumerate(self.members) if row != self.selected):
            self.error.configure(text="この従業員IDはすでに使われています。")
            return
        try:
            hourly_rate = int(rate)
        except ValueError:
            self.error.configure(text="時給の桁数が多すぎます。")
            return

        members = self.members.copy()
        index = self.selected if self.selected is not None else len(members)
        fields = {"employee_id": employee_id, "name": name, "hourly_rate_yen": hourly_rate}
        if index == len(members):
            members.append(fields)
        else:
            members[index] = {**members[index], **fields}
        self.commit(members, index)

    def delete_member(self):
        if self.selected is None:
            return
        index = self.selected
        member = self.members[index]
        if messagebox.askyesno("従業員の削除", f"{member['name']}（{member['employee_id']}）を削除しますか？", parent=self):
            self.commit(self.members[:index] + self.members[index + 1:], index)

    def commit(self, members, index):
        self.error.configure(text="")
        try:
            save_members(self.members, members)
        except (OSError, ValueError) as error:
            self.error.configure(text=str(error))
        else:
            self.members = members
            self.selected = None
            self.reload_table(index)
            self.on_saved()


class Panel(tk.Frame):
    def __init__(self, parent, title, columns=None, *, details=False, alert=False):
        super().__init__(
            parent, background=SURFACE, padx=12, pady=8, takefocus=False,
            highlightthickness="1.5p", highlightbackground=BORDER, highlightcolor=BORDER,
        )
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.alert = alert
        self.details = None
        self.items = {}
        self.table_rows = None
        self.indicator_state = None
        self.indicator_timer = None

        heading = ttk.Frame(self, style="Card.TFrame")
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.indicator_frame = ttk.Frame(heading, style="Indicator.TFrame", width=120, height=28)
        self.indicator_frame.grid(row=0, column=1, sticky="e", padx=(6, 0))
        self.indicator_content = ttk.Frame(self.indicator_frame, style="IndicatorContent.TFrame")
        self.indicator_icon = tk.Canvas(
            self.indicator_content, width=24, height=26, background=INDICATOR_BACKGROUND,
            borderwidth=0, highlightthickness=0,
        )
        self.indicator_icon.pack(side="left", padx=(0, 4))
        self.active_robot = create_robot_icon(self.indicator_icon)
        self.waiting_robot = create_robot_icon(self.indicator_icon, waiting=True)
        self.indicator_robot = self.indicator_icon.create_image(
            0, ROBOT_ICON_TOP, anchor="nw", image=self.active_robot,
        )
        self.indicator = ttk.Label(self.indicator_content, style="Indicator.TLabel")
        self.indicator.pack(side="left")

        self.toolbar = ttk.Frame(self, style="Card.TFrame")
        self.toolbar.grid(row=1, column=0, sticky="ew", pady=(3, 4))
        self.toolbar.columnconfigure(0, weight=1)
        self.caption = tk.StringVar()
        self.caption_label = ttk.Label(self.toolbar, textvariable=self.caption, style="Caption.TLabel")
        self.caption_label.grid(row=0, column=0, sticky="w")

        if columns is None:
            return

        table = ttk.Frame(self, style="Card.TFrame")
        table.grid(row=2, column=0, sticky="nsew")
        self.tree = create_table(table, columns, alert=alert)

        if details:
            detail_frame = ttk.Frame(self, style="Card.TFrame")
            detail_frame.grid(row=3, column=0, sticky="nsew", pady=(5, 0))
            detail_frame.columnconfigure(0, weight=1)
            self.rowconfigure(3, weight=1)
            self.details = tk.Text(
                detail_frame, wrap="word", height=3, width=1, padx=7, pady=4,
                state="disabled", relief="flat", background=INSET, foreground=INK,
                font="TkDefaultFont", selectbackground=ALERT if alert else ACCENT, selectforeground=SURFACE,
            )
            self.details.grid(row=0, column=0, sticky="nsew")
            scrollbar = ttk.Scrollbar(detail_frame, orient="vertical", command=self.details.yview)
            scrollbar.grid(row=0, column=1, sticky="ns")
            detail_frame.rowconfigure(0, weight=1)
            self.details.configure(yscrollcommand=scrollbar.set)
            self.tree.bind("<<TreeviewSelect>>", self.show_selection)

    def show_selection(self, _event=None):
        if self.details is None:
            return
        selection = self.tree.selection()
        detail = self.items.get(selection[0], "") if selection else ""
        if isinstance(detail, Path):
            detail = preview_file(detail)
        elif callable(detail):
            detail = detail()
        if detail == self.details.get("1.0", "end-1c"):
            return
        data_background = ALERT_BACKGROUND if self.alert else DATA_BACKGROUND
        self.details.configure(state="normal", background=data_background if detail else INSET)
        self.details.delete("1.0", "end")
        self.details.insert("1.0", detail)
        self.details.configure(state="disabled")

    def set_rows(self, rows, caption):
        self.caption.set(caption)
        caption_style = "Alert.TLabel" if self.alert else "Data.TLabel"
        self.caption_label.configure(style=caption_style if rows else "Caption.TLabel")
        self.items = {key: detail for key, _, detail in rows}
        table_rows = [(key, values) for key, values, _ in rows]
        if table_rows == self.table_rows:
            self.show_selection()
            return
        self.table_rows = table_rows
        selection = self.tree.selection()
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        for index, (key, values) in enumerate(table_rows):
            tags = ("alternate",) if index % 2 else ()
            self.tree.insert("", "end", iid=key, values=values, tags=tags)
        if self.items:
            selected = selection[0] if selection and selection[0] in self.items else next(iter(self.items))
            self.tree.selection_set(selected)
            self.tree.see(selected)
        self.show_selection()

    def set_indicator(self, label="", *, waiting=False):
        state = (label, waiting)
        if state == self.indicator_state:
            return
        self.indicator_state = state
        border = ACCENT if label and not waiting else BORDER
        self.configure(highlightbackground=border, highlightcolor=border)
        self.cancel_indicator_animation()
        if not label:
            self.indicator_content.place_forget()
            return

        self.indicator.configure(text=label)
        image = self.waiting_robot if waiting else self.active_robot
        self.indicator_icon.itemconfigure(self.indicator_robot, image=image)
        self.indicator_content.place(relx=0.5, rely=0.5, anchor="center")
        amplitude, period = (0.9, 3.6) if waiting else (1.5, 2.8)
        self.animate_indicator(monotonic(), amplitude, period)

    def animate_indicator(self, started_at, amplitude, period):
        elapsed = monotonic() - started_at
        top = ROBOT_ICON_TOP + round(amplitude * sin(tau * elapsed / period))
        self.indicator_icon.coords(self.indicator_robot, 0, top)
        self.indicator_timer = self.after(
            INDICATOR_ANIMATION_INTERVAL_MS, self.animate_indicator, started_at, amplitude, period,
        )

    def cancel_indicator_animation(self):
        if self.indicator_timer is not None:
            self.after_cancel(self.indicator_timer)
            self.indicator_timer = None

    def resize_indicator(self, display_font):
        label_width = max(display_font.measure(label) for _, label in NODE_INDICATORS.values())
        self.indicator_frame.configure(
            width=max(120, label_width + 38), height=max(28, display_font.metrics("linespace") + 6),
        )

    def destroy(self):
        self.cancel_indicator_animation()
        super().destroy()


class MonthlySummary(Panel):
    COLUMNS = (("従業員ID", 82, False), ("氏名", 95, True), ("勤務時間", 95, False), ("給与額（目安）", 112, False))

    def __init__(self, parent, display_font, edit_members):
        super().__init__(parent, "月別勤務・給与")
        self.display_font = display_font
        self.month = tk.StringVar(master=self)
        self.data = None
        self.toolbar.columnconfigure(0, weight=0)
        self.toolbar.columnconfigure(1, weight=1)
        self.month_picker = ttk.Combobox(self.toolbar, textvariable=self.month, state="readonly", width=9)
        self.month_picker.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.month_picker.bind("<<ComboboxSelected>>", self.change_month)
        self.caption_label.grid_configure(column=1, sticky="ew")
        self.edit_button = ttk.Button(self.toolbar, text="従業員リストを編集", command=edit_members)
        self.edit_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.toolbar.bind("<Configure>", self.resize_caption)

        self.views = ttk.Notebook(self)
        self.views.grid(row=2, column=0, sticky="nsew")
        self.views.bind("<<NotebookTabChanged>>", self.update_caption)

        roster = ttk.Frame(self.views, style="Card.TFrame", padding=3)
        roster.columnconfigure(0, weight=1)
        roster.rowconfigure(0, weight=1)
        self.views.add(roster, text="従業員一覧")
        table = ttk.Frame(roster, style="Card.TFrame")
        table.grid(row=0, column=0, sticky="nsew")
        self.tree = create_table(table, self.COLUMNS, horizontal=True)
        for column in ("column2", "column3"):
            self.tree.column(column, anchor="e")
        self.payroll_note = ttk.Label(roster, style="Caption.TLabel")
        self.payroll_note.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        roster.bind("<Configure>", lambda event: self.payroll_note.configure(wraplength=max(1, event.width - 6)))

        self.plot = ttk.Frame(self.views, style="Card.TFrame", padding=3)
        self.plot.columnconfigure(0, weight=1)
        self.plot.rowconfigure(0, weight=1)
        self.views.add(self.plot, text="勤務時間グラフ")

        plot = ttk.Frame(self.plot, style="Card.TFrame")
        plot.grid(row=0, column=0, sticky="nsew")
        plot.columnconfigure(0, weight=1)
        plot.rowconfigure(0, weight=1)
        self.chart = tk.Canvas(plot, height=80, width=1, background=SURFACE, highlightthickness=0)
        self.chart.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(plot, orient="vertical", command=self.chart.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.chart.configure(yscrollcommand=scrollbar.set)
        self.chart.bind("<Configure>", self.draw_chart)
        self.chart.bind("<MouseWheel>", self.scroll_chart)
        self.chart.bind("<Button-4>", self.scroll_chart)
        self.chart.bind("<Button-5>", self.scroll_chart)
        self.note = ttk.Label(self.plot, style="Caption.TLabel", text="開始・終了時刻だけの記録は休憩込みで集計します。")
        self.note.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.plot.bind("<Configure>", lambda event: self.note.configure(wraplength=max(1, event.width - 6)))

    def set_data(self, records, members):
        data = (records, members)
        if data == self.data:
            return
        self.data = data
        self.totals, self.incomplete, self.excluded, self.unmatched = monthly_totals(records or {}, members or [])
        totals = self.totals
        months = sorted((month for month in totals if month != UNDATED), reverse=True)
        if UNDATED in totals:
            months.append(UNDATED)
        if not months:
            months.append(datetime.now().strftime("%Y-%m"))
        self.month_picker.configure(values=months, state="disabled" if records is None else "readonly")
        if self.month.get() not in months:
            self.month.set(months[0])
            self.chart.yview_moveto(0)
        self.update_month()

    def change_month(self, _event=None):
        self.chart.yview_moveto(0)
        self.update_month()

    def update_month(self):
        records, members = self.data
        month = self.month.get()
        rows = payroll_rows(
            members or [], self.totals.get(month, {}), self.incomplete.get(month, set()), records_error=records is None,
        )
        self.set_rows(rows, "")
        note = ["給与額は勤務時間×現在の時給（1円未満四捨五入）。時刻の範囲は休憩込み。"]
        if self.unmatched[month]:
            note.append(f"未照合の勤怠 {self.unmatched[month]:,}件")
        if self.excluded[month]:
            note.append(f"時間を読めない勤怠 {self.excluded[month]:,}件")
        self.payroll_note.configure(text="  ｜  ".join(note))
        self.update_caption()
        self.draw_chart()

    def update_caption(self, _event=None):
        if self.data is None:
            return
        records, members = self.data
        month = self.month.get()
        if self.views.select() == str(self.plot):
            totals = self.totals.get(month, {})
            minutes_total = sum((minutes for _, minutes in totals.values()), Decimal(0))
            caption = f"{len(totals):,}人  ｜  総労働時間 {format_minutes(minutes_total)}"
            if self.excluded[month]:
                caption += f"  ｜  集計対象外 {self.excluded[month]:,}件"
            style = "Data.TLabel" if totals else "Caption.TLabel"
        else:
            caption = f"{len(members):,}人" if members else "従業員未登録"
            style = "Data.TLabel" if members else "Caption.TLabel"
            if members is None:
                caption, style = "従業員リストの読み込みエラー", "Alert.TLabel"
        if records is None:
            caption, style = "勤怠の読み込みエラー", "Alert.TLabel"
        self.caption.set(caption)
        self.caption_label.configure(style=style)

    def resize_caption(self, _event=None):
        available = self.toolbar.winfo_width() - self.month_picker.winfo_reqwidth() - self.edit_button.winfo_reqwidth() - 16
        self.caption_label.configure(wraplength=max(1, available))

    def resize_columns(self):
        samples = ("従業員ID", "氏名", "999時間59分", "給与額（目安）")
        for column, sample, (_, width, _) in zip(self.tree["columns"], samples, self.COLUMNS):
            minimum = self.display_font.measure(sample) + 14
            self.tree.column(column, minwidth=minimum, width=max(minimum, width))

    def scroll_chart(self, event):
        if event.num == 4 or event.delta > 0:
            self.chart.yview_scroll(-1, "units")
        elif event.num == 5 or event.delta < 0:
            self.chart.yview_scroll(1, "units")

    def draw_chart(self, _event=None):
        if self.data is None:
            return
        records, _ = self.data
        month = self.month.get()
        totals = self.totals.get(month, {})
        chart = self.chart
        width, height = chart.winfo_width(), chart.winfo_height()
        chart.delete("all")
        if width < 40:
            return
        row_height = self.display_font.metrics("linespace") + 8
        bar_half_height = max(5, row_height // 4)
        if not totals:
            chart.configure(scrollregion=(0, 0, width, height))
            message = "勤怠記録を確認してください" if records is None else "この月の集計対象データがありません"
            if month == UNDATED and records is not None:
                message = "集計できる勤務時間がありません"
            chart.create_text(width / 2, height / 2, width=width - 8, text=message, fill=MUTED, font=self.display_font)
            return

        rows = [
            (f"{worker}（{employee_id or '従業員ID未設定'}）", minutes)
            for (employee_id, _), (worker, minutes) in totals.items()
        ]
        rows.sort(key=lambda row: (-row[1], row[0].casefold()))
        max_minutes = rows[0][1] or Decimal(1)
        hours_width = max(self.display_font.measure(format_minutes(minutes)) for _, minutes in rows)
        name_width = max(1, min(max(self.display_font.measure(worker) for worker, _ in rows), width // 3))
        bar_left = name_width + 12
        bar_width = max(0, width - hours_width - bar_left - 12)
        top = 2
        for worker, minutes in rows:
            name = chart.create_text(
                0, top + 4, text=worker, width=name_width, anchor="nw", fill=INK, font=self.display_font,
            )
            _, name_top, _, name_bottom = chart.bbox(name)
            height_for_name = max(row_height, name_bottom - name_top + 8)
            center = top + height_for_name / 2
            if bar_width:
                bar_top, bar_bottom = center - bar_half_height, center + bar_half_height
                chart.create_rectangle(bar_left, bar_top, bar_left + bar_width, bar_bottom, fill=BORDER, outline="")
                if minutes:
                    bar_end = bar_left + bar_width * float(minutes / max_minutes)
                    chart.create_rectangle(bar_left, bar_top, bar_end, bar_bottom, fill=ACCENT, outline="")
            chart.create_text(
                width - 2, center, text=format_minutes(minutes), anchor="e", fill=DATA_INK, font=self.display_font,
            )
            top += height_for_name
        chart.configure(scrollregion=(0, 0, width, max(height, top + 2)), yscrollincrement=row_height)


class AttendanceApp:
    def __init__(self, root):
        self.root = root
        self.problem_targets = {}
        self.font_size = tk.IntVar(master=root, value=DEFAULT_FONT_SIZE)
        root.title("勤怠情報")
        width = root.winfo_screenwidth() * 85 // 100
        height = root.winfo_screenheight() * 85 // 100
        root.geometry(f"{width}x{height}")
        root.configure(background=BACKGROUND)
        self.configure_styles()

        main = ttk.Frame(root, padding=(14, 10), style="Main.TFrame")
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        header = ttk.Frame(main, style="Main.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="勤怠情報", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="文字サイズ", style="Status.TLabel").grid(row=0, column=1, sticky="e", padx=(0, 4))
        font_picker = ttk.Combobox(header, textvariable=self.font_size, values=FONT_SIZES, width=3, state="readonly")
        font_picker.grid(row=0, column=2, sticky="e", padx=(0, 8))
        font_picker.bind("<<ComboboxSelected>>", self.set_font_size)
        ttk.Button(header, text="再読み込み", command=self.refresh).grid(row=0, column=3, sticky="e")
        self.agent_status = tk.StringVar()
        ttk.Label(header, textvariable=self.agent_status, style="Status.TLabel").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 0),
        )

        cards = ttk.Frame(main, style="Main.TFrame")
        cards.grid(row=1, column=0, sticky="nsew")
        for index in range(2):
            cards.columnconfigure(index, weight=1, uniform="column")
            cards.rowconfigure(index, weight=1, uniform="row")

        file_columns = (("ファイル名", 215, True), ("形式", 62, False))
        problem_columns = (("種類", 95, False), ("対象ファイル", 165, True))
        self.pending = Panel(cards, "処理前勤怠情報", file_columns, details=True)
        self.processed = Panel(cards, "処理済み勤怠情報", file_columns, details=True)
        self.summary = MonthlySummary(cards, self.display_font, self.open_member_editor)
        self.problems = Panel(cards, "問題あり勤怠情報", problem_columns, details=True, alert=True)
        self.problems.tree.bind("<Double-1>", self.open_problem)
        self.panels = {
            "pending": self.pending, "processed": self.processed,
            "summary": self.summary, "problems": self.problems,
        }
        self.pending.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=(0, 4))
        self.processed.grid(row=0, column=1, sticky="nsew", padx=(4, 0), pady=(0, 4))
        self.summary.grid(row=1, column=0, sticky="nsew", padx=(0, 4), pady=(4, 0))
        self.problems.grid(row=1, column=1, sticky="nsew", padx=(4, 0), pady=(4, 0))

        add_button = ttk.Menubutton(self.pending.toolbar, text="情報を追加する", style="Add.TMenubutton")
        add_menu = tk.Menu(
            add_button, tearoff=False, font="TkMenuFont", background=SURFACE, foreground=INK,
            activebackground=BORDER, activeforeground=INK,
        )
        add_menu.add_command(label="ファイルを選ぶ", command=self.import_files)
        add_menu.add_command(label="カメラで撮影", command=self.open_camera)
        add_button.configure(menu=add_menu)
        add_button.grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.status = tk.StringVar()
        ttk.Label(main, textvariable=self.status, style="Status.TLabel").grid(
            row=2, column=0, sticky="ew", pady=(6, 0)
        )
        root.bind("<F5>", self.refresh)
        self.set_font_size()
        self.refresh_periodically()
        self.poll_agent()

    def configure_styles(self):
        self.style = style = ttk.Style(self.root)
        style.theme_use("clam")
        self.display_font = font.nametofont("TkDefaultFont", root=self.root)
        self.standard_fonts = [self.display_font] + [
            font.nametofont(name, root=self.root)
            for name in ("TkTextFont", "TkHeadingFont", "TkMenuFont", "TkFixedFont")
        ]
        family = next(
            (name for name in font.families(root=self.root) if name.casefold() == "meiryo ui"),
            self.display_font.actual("family"),
        )
        for standard_font in self.standard_fonts:
            standard_font.configure(family=family, size=self.font_size.get(), weight="normal")
        style.configure(
            ".", font=self.display_font, background=BACKGROUND, foreground=INK,
            bordercolor=BORDER, lightcolor=SURFACE, darkcolor=BORDER, troughcolor=INSET,
            selectbackground=BORDER, selectforeground=INK,
        )
        self.root.option_add("*TCombobox*Listbox.font", "TkDefaultFont")
        style.map(
            "TButton", background=[("disabled", INSET), ("pressed", PRESSED), ("active", BORDER)],
            lightcolor=[("pressed", BORDER)], darkcolor=[("pressed", BORDER)],
        )
        style.map(
            "TCombobox", background=[("pressed", PRESSED), ("active", BORDER)],
            fieldbackground=[("disabled", INSET), ("readonly", SURFACE)],
            foreground=[("disabled", MUTED)], bordercolor=[("focus", MUTED)],
        )
        style.configure("Main.TFrame", background=BACKGROUND)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("Title.TLabel", background=BACKGROUND, foreground=INK)
        style.configure("CardTitle.TLabel", background=SURFACE, foreground=INK)
        style.configure("Caption.TLabel", background=SURFACE, foreground=MUTED)
        style.configure("Data.TLabel", background=SURFACE, foreground=DATA_INK)
        style.configure("Alert.TLabel", background=SURFACE, foreground=ALERT)
        style.configure("Status.TLabel", background=BACKGROUND, foreground=MUTED)
        style.configure("Indicator.TFrame", background=INDICATOR_BACKGROUND, relief="solid", borderwidth=1)
        style.configure("IndicatorContent.TFrame", background=INDICATOR_BACKGROUND)
        style.configure("Indicator.TLabel", background=INDICATOR_BACKGROUND, foreground=ACCENT)
        style.configure("Add.TMenubutton", background=BACKGROUND, padding=(8, 3))
        style.map("Add.TMenubutton", background=[("pressed", PRESSED), ("active", BORDER)])
        style.configure(
            "Treeview", background=DATA_BACKGROUND, fieldbackground=SURFACE, foreground=DATA_INK, font=self.display_font,
        )
        style.configure("Problem.Treeview", background=ALERT_BACKGROUND, foreground=ALERT)
        style.configure("Treeview.Heading", background=BACKGROUND, foreground=INK, padding=(5, 3), font=self.display_font)
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", SURFACE)])
        style.map("Problem.Treeview", background=[("selected", ALERT)], foreground=[("selected", SURFACE)])

    def set_font_size(self, _event=None):
        size = self.font_size.get()
        for standard_font in self.standard_fonts:
            standard_font.configure(size=size)
        self.style.configure("Treeview", rowheight=self.display_font.metrics("linespace") + 5)
        extra = max(0, size - 8)
        self.root.minsize(*fit_window_size(self.root, 760 + 22 * extra, 560 + 10 * extra))
        for panel in self.panels.values():
            panel.resize_indicator(self.display_font)
        problem_type_width = max(95, self.display_font.measure("問題の記録") + 14)
        self.problems.tree.column("column0", width=problem_type_width, minwidth=problem_type_width)
        self.summary.resize_columns()
        self.root.after_idle(self.summary.resize_caption)
        self.summary.draw_chart()

    def import_files(self):
        filenames = filedialog.askopenfilenames(parent=self.root, title="追加するファイルを選ぶ")
        if not filenames:
            return

        added = 0
        errors = []
        for filename in filenames:
            source = Path(filename)
            try:
                add_input_file(source.name, partial(shutil.copyfile, source))
            except OSError as error:
                errors.append(f"{source.name}：{error.strerror or error}")
            else:
                added += 1

        self.refresh()
        if added:
            self.status.set(f"{added:,}件のファイルを追加しました。")
        if errors:
            messagebox.showerror(
                "ファイルの追加", f"{added:,}件を追加しました。\n追加できなかったファイル\n\n" + "\n".join(errors),
                parent=self.root,
            )

    def open_camera(self):
        try:
            import cv2
        except ImportError:
            python_command = f'& "{sys.executable}"' if sys.platform == "win32" else f'"{sys.executable}"'
            messagebox.showerror(
                "カメラで撮影",
                "カメラを使うには OpenCV が必要です。\nアプリのフォルダーで次のコマンドを実行してください。\n\n"
                f"{python_command} -m pip install -r requirements.txt",
                parent=self.root,
            )
        else:
            CameraDialog(self.root, cv2, self.save_photo)

    def save_photo(self, photo):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = add_input_file(f"撮影_{timestamp}.jpg", lambda path: path.write_bytes(photo))
        self.refresh()
        self.status.set(f"{target.name} を追加しました。")

    def open_problem(self, event):
        tree = self.problems.tree
        if tree.identify_region(event.x, event.y) != "cell":
            return
        row = tree.identify_row(event.y)
        path, findings = self.problem_targets.get(row, (None, ""))
        if path is None:
            messagebox.showinfo("勤怠データの修正", "対象の保管ファイルが見つかりません。", parent=self.root)
        elif path.suffix.lower() != ".json":
            messagebox.showinfo("勤怠データの修正", "未対応の形式です。", parent=self.root)
        else:
            try:
                source, original_digest = read_json_for_repair(path)
            except (OSError, ValueError) as error:
                messagebox.showerror("JSONを開けませんでした", str(error), parent=self.root)
            else:
                JsonRepairDialog(self.root, path, source, original_digest, findings, self.on_problem_submitted)
        return "break"

    def on_problem_submitted(self, target):
        self.refresh()
        self.status.set(f"{target.name} を処理前勤怠情報に戻しました。")

    def open_member_editor(self):
        try:
            members = read_members(default=[])
        except (OSError, ValueError) as error:
            messagebox.showerror("従業員リストの編集", str(error), parent=self.root)
        else:
            MemberEditor(self.root, members, self.refresh)

    def refresh_periodically(self):
        self.refresh()
        self.root.after(DATA_INTERVAL_MS, self.refresh_periodically)

    def poll_agent(self):
        phase, node, message = read_agent_status()
        self.agent_status.set(message)
        target, label = None, ""
        if phase == "active":
            target, label = NODE_INDICATORS.get(node, (None, ""))
        elif phase == "waiting":
            target, label = "pending", "入力待ち"
        for name, panel in self.panels.items():
            panel.set_indicator(label if name == target else "", waiting=name == target and phase == "waiting")
        self.root.after(STATUS_INTERVAL_MS, self.poll_agent)

    def refresh(self, _event=None):
        errors = []
        links_by_file = None
        try:
            records = read_record_details()
        except (OSError, ValueError) as error:
            records = None
            errors.append(f"records.json  {error}")
        else:
            links_by_file = {}
            for record in records.values():
                for archived_name, outcome in record["source_files"].items():
                    link = (
                        record["id"], record.get("employee_id"), record["worker"],
                        record["work_period"], record["work_hours"], outcome,
                    )
                    links_by_file.setdefault(archived_name, []).append(link)

        try:
            members = read_members(default=[])
        except (OSError, ValueError) as error:
            members = None
            errors.append(str(error))
        self.summary.set_data(records, members)

        for panel, directory in ((self.pending, INPUT_DIR), (self.processed, INPUT_OLD_DIR)):
            try:
                files = list_files(directory)
            except OSError as error:
                panel.set_rows([], "読み込みエラー")
                errors.append(f"{directory.name}  {error.strerror or error}")
            else:
                rows = []
                for path in files:
                    detail = path
                    if panel is self.processed:
                        links = links_by_file.get(path.name, []) if links_by_file is not None else None
                        detail = partial(processed_file_detail, path, links)
                    rows.append((path.name, (path.name, path.suffix.lstrip(".").upper() or "—"), detail))
                panel.set_rows(rows, f"{len(files):,}ファイル" if files else "対象ファイルなし")

        self.refresh_problems(errors)
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.status.set("　｜　".join([f"最終読み込み {timestamp}", *errors]))

    def refresh_problems(self, errors):
        read_errors = []
        # 保管名は原本の移動前に記録されるため、保管先を先に読み込む。
        try:
            archive = {path.name: path for path in list_files(INPUT_PROBLEM_DIR)}
        except OSError as error:
            archive = {}
            read_errors.append("保管ファイル 読み込みエラー")
            errors.append(f"{INPUT_PROBLEM_DIR.name}  {error.strerror or error}")

        try:
            completed = {path.name for path in list_files(INPUT_PROBLEM_DIR / RESOLVED_PROBLEM_SUBDIR)}
        except OSError as error:
            completed = set()
            read_errors.append("対応済みファイル 読み込みエラー")
            errors.append(f"{RESOLVED_PROBLEM_SUBDIR}  {error.strerror or error}")

        try:
            problems = read_problems()
        except (OSError, ValueError) as error:
            problems = []
            read_errors.append("問題の記録 読み込みエラー")
            errors.append(f"problems.json  {error}")

        entries = {}
        for index, problem in enumerate(problems):
            archive_names = problem.get("archive_names")
            requested = problem.get("resubmission_requests", [])
            for name in dict.fromkeys(problem["files"] or [None]):
                archived = name if archive_names is None else archive_names.get(name)
                if archived not in archive and (archived in requested or archived in completed):
                    continue
                if archived:
                    key = f"file-{archived}"
                elif name:
                    key = f"problem-file-{name}"
                else:
                    key = f"problem-{index}"
                _, _, details = entries.setdefault(key, (archived or name or "—", archive.get(archived), []))
                details.append(f"対象  {name or '—'}\n理由  {problem['reason']}\n詳細  {problem['description']}")

        rows = []
        targets = {}
        for key, (name, path, details) in entries.items():
            text = "\n\n".join(details)
            detail = partial(problem_file_detail, path, text) if path else text
            rows.append((key, ("問題の記録", name), detail))
            targets[key] = (path, text)
        for name, path in archive.items():
            key = f"file-{name}"
            if key not in entries:
                rows.append((key, ("保管ファイル", name), path))
                targets[key] = (path, "指摘内容の記録が見つかりません。")
        self.problem_targets = targets
        self.problems.set_rows(rows, "　｜　".join([f"{len(rows):,}件", *read_errors]))


def main():
    is_windows = sys.platform == "win32"
    if is_windows:
        from ctypes import windll

        # Tk 8.6 uses one application-wide scaling factor for fonts and geometry.
        windll.user32.SetProcessDPIAware()
        windll.shell32.SetCurrentProcessExplicitAppUserModelID("AttendanceAgent.Desktop")

    root = tk.Tk()
    if is_windows:
        root.iconphoto(True, create_robot_icon(root))
    AttendanceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
