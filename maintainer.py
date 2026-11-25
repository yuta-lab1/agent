"""エージェントのツール定義を閲覧し、採番付きの下書きを追加する。"""

import argparse
import ast
import builtins
import io
import json
import keyword
import os
import re
import stat
import symtable
import sys
import tkinter as tk
import tokenize
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from tkinter import font, messagebox, ttk


TOOL_LABEL = re.compile(r"#\s*\[([0-9]+)\]\[tool\]\s*(\S+)\s*")
FUNCTION_NUMBER = re.compile(r"#\s*\[(\d+)\]\[(?:tool|support)\]")
SECTION_LABEL = re.compile(
    r"#\s*(0\.\s*変数定義|1\.\s*基本処理|2\.\s*state\s*更新)"
    r"\s*(?:[：:#]\s*(.*))?$"
)


@dataclass(frozen=True)
class ToolInfo:
    number: str
    name: str
    line: int
    overview: str | None
    state_overview: str | None
    state_rows: tuple[tuple[str, str, str], ...]
    variables: str = ""
    status: str = "判定不可"


def tool_registry(module: ast.Module) -> dict[str, str] | None:
    """現在の loop.define_tools と旧形式の TOOLS の静的な登録表を読む。"""
    loops = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "loop"]
    registries = []
    if loops:
        for node in loops[-1].body:
            if isinstance(node, ast.FunctionDef) and node.name == "define_tools":
                registries = [stmt.value for stmt in ast.walk(node) if isinstance(stmt, ast.Return)]
                break
    if not registries:
        registries = [node.value for node in module.body if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets)]
    if len(registries) != 1 or not isinstance(registries[0], ast.Dict):
        return None
    registry = {}
    for key, value in zip(registries[0].keys, registries[0].values):
        if (not isinstance(key, ast.Constant) or not isinstance(key.value, str)
                or not isinstance(value, (ast.Tuple, ast.List)) or not value.elts
                or not isinstance(value.elts[0], ast.Name)):
            return None
        registry[key.value] = value.elts[0].id
    return registry


def read_tools(path: Path) -> list[ToolInfo]:
    """Python を実行せず、関数定義とコメントを対応付ける。"""
    with tokenize.open(path) as file:
        source = file.read()
    module = ast.parse(source, filename=str(path))
    registry = tool_registry(module)
    lines = source.split("\n")
    comments = {
        token.start[0]: token
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }

    tools = []
    for function in module.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = function.decorator_list[0].lineno if function.decorator_list else function.lineno
        label = comments.get(start - 1)
        if label is None or label.start[1] != function.col_offset:
            continue
        match = TOOL_LABEL.fullmatch(label.string)
        if match is None or match[2] != function.name:
            continue

        markers = []
        summaries = {}
        indent = function.body[0].col_offset
        # AST の end_lineno は最終文の行。下書きではその後にも state 更新コメントがある。
        end = function.end_lineno
        while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t"))):
            end += 1
        for line in range(function.lineno + 1, end + 1):
            comment = comments.get(line)
            if comment is None or len(comment.line) - len(comment.line.lstrip()) != indent:
                continue
            section = SECTION_LABEL.fullmatch(comment.string)
            if section is None:
                continue
            name = re.sub(r"\s+", "", section[1])
            markers.append((line, name))
            if section[2]:
                try:
                    payload = json.loads(section[2])
                except json.JSONDecodeError:
                    continue
                key = {"1.基本処理": "機能概要", "2.state更新": "state更新概要"}.get(name)
                value = payload.get(key) if isinstance(payload, dict) else None
                if isinstance(value, str) and value.strip():
                    summaries[name] = value

        variables = []
        state_rows = []
        section_name = None
        sections = iter(markers)
        upcoming = next(sections, None)
        for statement in function.body:
            while upcoming and upcoming[0] <= statement.lineno:
                section_name = upcoming[1]
                upcoming = next(sections, None)
            if section_name == "0.変数定義":
                variables.append(ast.get_source_segment(source, statement) or ast.unparse(statement))
            elif section_name == "2.state更新" and isinstance(statement, ast.Assign):
                if not any(isinstance(target, ast.Name) and target.id == "state"
                           for target in statement.targets):
                    continue
                if not isinstance(statement.value, ast.Dict):
                    continue
                for key, value in zip(statement.value.keys, statement.value.values):
                    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                        continue
                    if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name) and value.value.id == "current":
                        handling = "引き継ぎ"
                    elif (
                        isinstance(value, ast.Constant) and value.value is None
                        or isinstance(value, ast.List) and not value.elts
                        or isinstance(value, ast.Dict) and not value.keys
                    ):
                        handling = "消去"
                    else:
                        handling = "更新"
                    state_rows.append((key.value, handling, ast.unparse(value)))

        status = "判定不可"
        if registry is not None:
            status = "登録済み" if function.name in registry.values() else "下書き"
        tools.append(ToolInfo(match[1], function.name, function.lineno,
                              summaries.get("1.基本処理"), summaries.get("2.state更新"), tuple(state_rows),
                              "\n".join(variables), status))
    return sorted(tools, key=lambda tool: int(tool.number))


def function_numbers(source: str):
    for token in tokenize.generate_tokens(io.StringIO(source, newline=None).readline):
        if token.type == tokenize.COMMENT:
            match = FUNCTION_NUMBER.match(token.string)
            if match:
                yield int(match[1])


def add_draft(path: Path, name: str, overview: str) -> None:
    name, overview = name.strip(), overview.strip()
    if (not name.isidentifier() or keyword.iskeyword(name) or hasattr(builtins, name)
            or name.startswith("__") or unicodedata.normalize("NFKC", name) != name):
        raise ValueError("ツール名には、Python の関数名に使える名前を入力してください（例：check_overtime）。")
    if not overview:
        raise ValueError("機能概要を入力してください。")

    lock_path = path.with_name(f".{path.name}.viewer.lock")
    try:
        lock = lock_path.open("xb")
    except FileExistsError:
        raise ValueError(f"保存ロックがあります。ほかの viewer の保存完了後に再度登録してください。\n{lock_path}") from None
    temp_path = None
    try:
        with lock:
            original = path.read_bytes()
            encoding, _ = tokenize.detect_encoding(io.BytesIO(original).readline)
            source = original.decode(encoding)
            module = ast.parse(source, filename=str(path))
            loops = [f for f in module.body if isinstance(f, ast.FunctionDef) and f.name == "loop"]
            if len(loops) != 1:
                raise ValueError("下書きの挿入位置を特定できません。トップレベルの loop 関数を確認してください。")
            registry = tool_registry(module)
            if name in symtable.symtable(source, str(path), "exec").get_identifiers() or (registry and name in registry):
                raise ValueError(f"「{name}」はこのファイルですでに使われています。")

            next_number = max(function_numbers(source), default=0) + 1

            # loop の採番・説明コメントとデコレータは元の関数に付けたままにする。
            loop = loops[0]
            lines = list(io.StringIO(source, newline=""))
            pos = min([loop.lineno] + [d.lineno for d in loop.decorator_list]) - 1
            while pos and lines[pos - 1].startswith("#"):
                if pos <= 2 and re.match(r"^#(?:!|.*?coding[:=])", lines[pos - 1]):
                    break
                pos -= 1
            newline = next((line[len(line.rstrip("\r\n")):] for line in lines if line.endswith(("\r", "\n"))), "\n")
            summary = json.dumps({"機能概要": overview}, ensure_ascii=False, separators=(",", ":"))
            draft = [
                f"#[{next_number:03d}][tool]{name}",
                f"def {name}():",
                "    #0.変数定義",
                "",
                f"    #1.基本処理：{summary}",
                "    pass",
                "",
                '    #2.state更新：{"state更新概要":""}',
                "", "", "",
            ]
            updated = "".join(lines[:pos]) + newline.join(draft) + "".join(lines[pos:])
            compile(updated, str(path), "exec", dont_inherit=True)
            with NamedTemporaryFile(dir=path.parent, delete=False) as output:
                temp_path = Path(output.name)
                output.write(updated.encode(encoding))
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp_path, stat.S_IMODE(path.stat().st_mode))
            if path.read_bytes() != original:
                raise ValueError("保存中にファイルが更新されました。再読み込みしてから登録してください。")
            os.replace(temp_path, path)
    finally:
        lock_path.unlink(missing_ok=True)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def show_viewer(root: tk.Tk, path: Path, tools: list[ToolInfo]) -> None:
    root.title(f"{path.name} — Agent Viewer")
    paper, surface, ink, muted, edge, accent = "#f1f3f4", "#ffffff", "#19272b", "#4d6165", "#d2dbdb", "#105b61"
    scale = root.winfo_fpixels("1i") / 96

    def px(size):
        return max(1, round(size * scale))

    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{min(px(1580), int(sw * .95))}x{min(px(1000), int(sh * .88))}")
    root.minsize(min(px(780), int(sw * .9)), min(px(500), int(sh * .8)))
    root.configure(background=paper)
    style = ttk.Style(root)
    style.theme_use("clam")
    base = font.nametofont("TkDefaultFont")
    base.configure(size=max(10, int(base.cget("size"))))
    family = base.actual("family")
    mono = font.nametofont("TkFixedFont").actual("family")
    row_height = base.metrics("linespace") + px(12)
    style.configure(".", font=base, background=surface, foreground=ink)
    style.configure("Bar.TFrame", background=ink)
    style.configure("Bar.TLabel", background=ink, foreground=surface, font=(family, 16, "bold"))
    style.configure("Title.TLabel", font=(family, 16, "bold"))
    style.configure("Section.TLabel", font=(family, 11, "bold"))
    style.configure("Meta.TLabel", foreground=muted)
    style.configure("Registered.TLabel", background="#e5efee", foreground=accent, padding=(8, 3))
    style.configure("Draft.TLabel", background=paper, foreground=muted, padding=(8, 3))
    style.configure("TButton", padding=(12, 6))
    style.configure("Primary.TButton", background=accent, foreground=surface)
    style.map("Primary.TButton", background=[("active", "#16484e")], foreground=[("active", surface)])
    style.configure("Treeview", rowheight=row_height, fieldbackground=surface, borderwidth=0)
    style.configure("Treeview.Heading", background=paper, foreground=muted, relief="flat",
                    padding=(5, 5), font=(family, 10, "bold"))
    style.map("Treeview", background=[("selected", "#d6e8e7")], foreground=[("selected", ink)])
    style.configure("TPanedwindow", background=edge)
    style.configure("Sash", sashthickness=px(6))

    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)
    bar = ttk.Frame(root, style="Bar.TFrame", padding=(px(18), px(12)))
    bar.grid(sticky="ew")
    bar.columnconfigure(1, weight=1)
    ttk.Label(bar, text="Agent Viewer", style="Bar.TLabel").grid(row=0, column=0, sticky="w")
    source = ttk.Entry(bar)
    source.insert(0, str(path))
    source.configure(state="readonly")
    source.grid(row=0, column=1, sticky="ew", padx=px(15))

    panes = ttk.Panedwindow(root, orient="horizontal")
    panes.grid(row=1, column=0, sticky="nsew", padx=px(12), pady=px(12))
    nav = ttk.Frame(panes, padding=px(10), width=px(360))
    detail = ttk.Frame(panes, padding=px(14))
    panes.add(nav, weight=1)
    panes.add(detail, weight=3)
    nav.columnconfigure(0, weight=1)
    nav.rowconfigure(1, weight=1)
    nav_title = ttk.Label(nav, style="Section.TLabel")
    nav_title.grid(sticky="w", pady=(0, px(10)))

    def table(parent, columns, height):
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=tuple(c[0] for c in columns), show="headings",
                            selectmode="browse", height=height)
        for key, title, width, stretch, anchor in columns:
            tree.heading(key, text=title, anchor=anchor)
            tree.column(key, width=px(width), minwidth=px(min(width, 85)), stretch=stretch, anchor=anchor)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        tree.tag_configure("stripe", background="#f6f8f8")
        return frame, tree

    grid, listing = table(nav, [("number", "番号", 48, False, "center"), ("name", "ツール名", 174, True, "w"),
                                ("status", "定義状態", 88, False, "center")], 12)
    grid.grid(row=1, column=0, sticky="nsew")
    detail.columnconfigure(0, weight=1)
    detail.rowconfigure(2, weight=1)
    heading = ttk.Frame(detail)
    heading.grid(row=0, column=0, sticky="ew")
    title = ttk.Label(heading, style="Title.TLabel")
    title.pack(side="left")
    badge = ttk.Label(heading)
    badge.pack(side="left", padx=px(12))
    location = ttk.Label(detail, style="Meta.TLabel")
    location.grid(row=1, column=0, sticky="w", pady=(2, px(12)))

    sections = ttk.Panedwindow(detail, orient="vertical")
    sections.grid(row=2, column=0, sticky="nsew")
    summary_panel = ttk.Frame(sections)
    state_panel = ttk.Frame(sections, padding=(0, px(10), 0, 0))
    sections.add(summary_panel, weight=1)
    sections.add(state_panel, weight=3)
    summary_panel.columnconfigure(0, weight=1)
    summary_panel.rowconfigure(0, weight=1)
    canvas = tk.Canvas(summary_panel, bg=surface, highlightthickness=0, width=1, height=px(205))
    canvas.grid(row=0, column=0, sticky="nsew")
    scroll = ttk.Scrollbar(summary_panel, orient="vertical", command=canvas.yview)
    scroll.grid(row=0, column=1, sticky="ns")
    canvas.configure(yscrollcommand=scroll.set)
    overview = ttk.Frame(canvas)
    window = canvas.create_window(0, 0, window=overview, anchor="nw")
    overview.columnconfigure(1, weight=1)
    contents = []
    for i, text in enumerate(("0. 変数定義", "1. 基本処理", "2. state更新")):
        ttk.Separator(overview).grid(row=2 * i, column=0, columnspan=2, sticky="ew")
        ttk.Label(overview, text=text, style="Meta.TLabel", anchor="nw").grid(
            row=2 * i + 1, column=0, sticky="nsw", padx=(0, px(14)), pady=px(9))
        cell = ttk.Label(overview, anchor="nw", justify="left", width=1, wraplength=px(540),
                         font=(mono, 10) if i == 0 else base)
        cell.grid(row=2 * i + 1, column=1, sticky="ew", pady=px(9))
        contents.append(cell)

    def resize_overview(event):
        canvas.itemconfigure(window, width=event.width)
        label_width = overview.grid_bbox(0, 1)[2]
        for cell in contents:
            cell.configure(wraplength=max(px(60), event.width - label_width - px(8)))

    canvas.bind("<Configure>", resize_overview)
    overview.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))

    def scroll_overview(event):
        if canvas.yview() != (0.0, 1.0):
            if event.num == 4 or event.delta > 0:
                canvas.yview_scroll(-2, "units")
            else:
                canvas.yview_scroll(2, "units")

    for widget in (canvas, overview, *overview.winfo_children()):
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(sequence, scroll_overview)
    state_panel.columnconfigure(0, weight=1)
    state_panel.rowconfigure(1, weight=1)
    state_title = ttk.Label(state_panel, style="Section.TLabel")
    state_title.grid(sticky="w", pady=(0, px(8)))
    grid, state_table = table(state_panel, [("key", "キー", 160, False, "w"), ("value", "値", 460, True, "w"),
                                            ("handling", "扱い", 88, False, "center")], 16)
    grid.grid(row=1, column=0, sticky="nsew")

    def selected_tool():
        selection = listing.selection()
        return tools[int(selection[0])] if selection else None

    def show_selected(_event=None):
        tool = selected_tool()
        state_table.delete(*state_table.get_children())
        title.configure(text=tool.name if tool else "ツールがありません")
        badge.configure(text=tool.status if tool else "—",
                        style="Registered.TLabel" if tool and tool.status == "登録済み" else "Draft.TLabel")
        location.configure(text=f"No. {tool.number}   /   {path.name}   {tool.line} 行" if tool else "")
        values = (tool.variables, tool.overview, tool.state_overview) if tool else ("", "", "")
        for cell, value in zip(contents, values):
            cell.configure(text=value or "—")
        canvas.yview_moveto(0)
        rows = tool.state_rows if tool else ()
        state_title.configure(text=f"state 更新   {len(rows)} 項目")
        for column, index, minimum in (("key", 0, 140), ("value", 2, 160)):
            required = max((base.measure(row[index]) + px(18) for row in rows), default=0)
            width = max(required, px(minimum))
            state_table.column(column, minwidth=width, width=width)
        for i, (key, handling, value) in enumerate(rows):
            state_table.insert("", "end", values=(key, value, handling), tags=("stripe",) if i % 2 else ())
        state_table.xview_moveto(0)
        state_table.yview_moveto(0)

    def populate(new_tools, preferred=None):
        nonlocal tools
        listing.delete(*listing.get_children())
        tools = new_tools
        nav_title.configure(text=f"ツール一覧   {len(tools)} 件")
        name_width = max((base.measure(tool.name) + px(18) for tool in tools), default=0)
        listing.column("name", minwidth=max(px(140), name_width), width=max(px(174), name_width))
        selected = "0"
        for i, tool in enumerate(tools):
            iid = str(i)
            listing.insert("", "end", iid=iid, values=(tool.number, tool.name, tool.status),
                           tags=("stripe",) if i % 2 else ())
            if tool.name == preferred:
                selected = iid
        if tools:
            listing.selection_set(selected)
            listing.focus(selected)
            listing.see(selected)
        show_selected()

    def reload_tools(_event=None):
        current = selected_tool()
        try:
            new_tools = read_tools(path)
        except (OSError, SyntaxError, UnicodeError, tokenize.TokenError, ValueError) as exc:
            messagebox.showerror("再読み込みできません", str(exc), parent=root)
            return
        populate(new_tools, current.name if current else None)

    def open_draft():
        dialog = tk.Toplevel(root)
        dialog.withdraw()
        dialog.title("ツールを追加")
        dialog.transient(root)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        form = ttk.Frame(dialog, padding=px(20))
        form.grid(sticky="nsew")
        form.columnconfigure(0, weight=1)
        form.rowconfigure(3, weight=1)
        ttk.Label(form, text="ツール名", style="Section.TLabel").grid(row=0, sticky="w")
        name = ttk.Entry(form, width=42)
        name.grid(row=1, sticky="ew", pady=(px(5), px(14)))
        ttk.Label(form, text="機能概要", style="Section.TLabel").grid(row=2, sticky="w")
        summary = tk.Text(form, width=46, height=5, wrap="char", font=base, relief="solid", borderwidth=1,
                          padx=px(7), pady=px(7), undo=True)
        summary.grid(row=3, sticky="nsew", pady=(px(5), px(12)))
        hint = ttk.Label(form, text=f"{path.name} に下書きとして追加します。", wraplength=px(400), style="Meta.TLabel")
        hint.grid(row=4, sticky="w")
        actions = ttk.Frame(form)
        actions.grid(row=5, sticky="e", pady=(px(16), 0))

        def save():
            tool_name = name.get().strip()
            try:
                add_draft(path, tool_name, summary.get("1.0", "end-1c"))
            except (OSError, SyntaxError, UnicodeError, tokenize.TokenError, ValueError) as exc:
                messagebox.showerror("登録できません", str(exc), parent=dialog)
                return
            dialog.destroy()
            try:
                populate(read_tools(path), tool_name)
            except (OSError, SyntaxError, UnicodeError, tokenize.TokenError, ValueError) as exc:
                messagebox.showerror("下書きの保存後に再読み込みできません", str(exc), parent=root)

        ttk.Button(actions, text="キャンセル", command=dialog.destroy).pack(side="left", padx=(0, px(8)))
        ttk.Button(actions, text="下書きを登録", style="Primary.TButton", command=save).pack(side="left")
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - dialog.winfo_reqheight()) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.deiconify()
        dialog.wait_visibility()
        dialog.grab_set()
        name.focus_set()

    ttk.Button(bar, text="再読み込み", command=reload_tools).grid(row=0, column=2, padx=(0, px(8)))
    ttk.Button(bar, text="＋ ツールを追加", style="Primary.TButton", command=open_draft).grid(row=0, column=3)
    listing.bind("<<TreeviewSelect>>", show_selected)
    root.bind("<F5>", reload_tools)
    populate(tools)
    listing.focus_set()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="対象の Python ファイル（例: agent.py）")
    args = parser.parse_args()
    try:
        path = args.file.expanduser().resolve()
        tools = read_tools(path)
    except (OSError, SyntaxError, UnicodeError, tokenize.TokenError) as error:
        parser.exit(1, f"Python ファイルを読み込めませんでした: {error}\n")

    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            ctypes.windll.user32.SetProcessDPIAware()
    try:
        root = tk.Tk()
    except tk.TclError as error:
        parser.exit(1, f"GUI を起動できませんでした: {error}\n")
    show_viewer(root, path, tools)
    root.mainloop()


if __name__ == "__main__":
    main()
