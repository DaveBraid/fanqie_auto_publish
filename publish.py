import argparse
import glob
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


STATE_FILE = "state.json"
CHAPTERS_DIR = "chapters"
UPLOADED_DIR = "uploaded"

BOOK_MANAGE_URL = "https://fanqienovel.com/main/writer/book-manage"

DEFAULT_INTERACTIVE_BOOK = "死人请柬"
DEFAULT_INTERACTIVE_COUNT = 1
DEFAULT_INTERACTIVE_VOLUME_NUM = 1
DEFAULT_INTERACTIVE_SCHEDULE_TIME = "18:00"


@dataclass
class BookQueue:
    name: str
    chapter_dir: Path
    txt_files: list[Path]


@dataclass
class PublishPlan:
    book_name: str
    txt_files: list[Path]
    volume_num: int | None
    volume_name: str | None
    volume_dir: Path
    schedule_time: str | None
    ai_used: str
    use_basic_check: bool
    dry_run: bool
    headless: bool
    non_interactive: bool
    keep_open: bool


def get_playwright_proxy():
    proxy_url = (
        os.environ.get("PLAYWRIGHT_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("http_proxy")
    )
    if not proxy_url:
        return None
    return {"server": proxy_url}


def chapter_file_sort_key(path: Path) -> tuple[int, str]:
    text = path.stem
    match = re.search(r"第\s*0*(\d+)\s*章", text)
    if match:
        return int(match.group(1)), text
    match = re.search(r"0*(\d+)", text)
    if match:
        return int(match.group(1)), text
    return 999999, text


def cn_volume_name(volume_num: int | None) -> str | None:
    if volume_num is None:
        return None
    cn_digits = "一二三四五六七八九十"
    if 1 <= volume_num <= 10:
        return f"第{cn_digits[volume_num - 1]}卷"
    return f"第{volume_num}卷"


def scan_book_queues(chapters_dir: Path) -> list[BookQueue]:
    root_txt_files = sorted(chapters_dir.glob("*.txt")) if chapters_dir.is_dir() else []
    if root_txt_files:
        print(f"\n[提示] 发现 {chapters_dir}/ 根目录下有 {len(root_txt_files)} 个散落的 txt 文件。")
        print("       多部小说管理模式要求章节放在子目录中，例如：")
        print("         chapters/死人请柬/第001章_死人请柬.txt")
        print("       请先将这些文件移入对应的书名子目录后再运行脚本。\n")
        raise SystemExit(2)

    book_dirs: list[BookQueue] = []
    if chapters_dir.is_dir():
        for name in sorted(os.listdir(chapters_dir)):
            sub_path = chapters_dir / name
            if sub_path.is_dir():
                txts = sorted(sub_path.glob("*.txt"), key=chapter_file_sort_key)
                if txts:
                    book_dirs.append(BookQueue(name, sub_path, txts))
    return book_dirs


def find_book_queue(book_dirs: list[BookQueue], book_name: str) -> BookQueue | None:
    for book in book_dirs:
        if book.name == book_name:
            return book
    return None


def prompt_input(message: str, default: str = "") -> str:
    try:
        return input(message)
    except EOFError:
        print()
        return default


def parse_positive_int(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError(f"{label} 必须是数字")
    if parsed <= 0:
        raise ValueError(f"{label} 必须大于 0")
    return parsed


def choose_book_interactively(book_dirs: list[BookQueue], default_book: str) -> BookQueue:
    print("\n==================================================")
    print("即将开始【全自动】发文！多部小说隔离管理模式已启动！")
    print("==================================================")
    print("\n检测到以下小说有待发章节：\n")
    default_idx = 0
    for idx, book in enumerate(book_dirs, 1):
        if book.name == default_book:
            default_idx = idx - 1
        marker = " [默认]" if book.name == default_book else ""
        print(f"  [{idx}] {book.name}  （{len(book.txt_files)} 章待发）{marker}")
    print()

    choice = prompt_input(f">>> 请输入序号选择要发布的小说（直接回车默认 {book_dirs[default_idx].name}）：").strip()
    if not choice:
        return book_dirs[default_idx]
    choice_idx = parse_positive_int(choice, "小说序号") - 1
    if choice_idx < 0 or choice_idx >= len(book_dirs):
        raise ValueError("无效的小说序号")
    return book_dirs[choice_idx]


def prompt_publish_count(total_chapters: int, default_count: int) -> int:
    safe_default = min(default_count, total_chapters)
    raw = prompt_input(
        f"\n>>> 请输入本次要发布的章节数量（1-{total_chapters}，直接回车默认 {safe_default}）："
    ).strip()
    if not raw:
        return safe_default
    count = parse_positive_int(raw, "章节数量")
    return min(count, total_chapters)


def prompt_volume_num(default_volume: int) -> int | None:
    raw = prompt_input(
        f">>> 请输入本次发布的章节属于第几卷（直接回车默认 {default_volume}，输入 0 则不切换分卷）："
    ).strip()
    if not raw:
        return default_volume
    volume = int(raw)
    if volume < 0:
        raise ValueError("卷号不能小于 0")
    return volume or None


def build_plan(args: argparse.Namespace) -> PublishPlan:
    if not Path(STATE_FILE).exists() and not args.dry_run:
        raise FileNotFoundError(f"找不到登录状态文件 {STATE_FILE}，请先运行 conda run -n fanqie python login.py 登录")

    chapters_dir = Path(args.chapters_dir)
    uploaded_dir = Path(args.uploaded_dir)
    book_dirs = scan_book_queues(chapters_dir)
    if not book_dirs:
        raise FileNotFoundError(f"{chapters_dir}/ 中没有找到任何待发章节")

    non_interactive = bool(args.cli)
    if non_interactive:
        missing = []
        if not args.book:
            missing.append("--book")
        if args.count is None:
            missing.append("--count")
        if args.volume is None:
            missing.append("--volume")
        if not args.no_schedule and not args.schedule_time:
            missing.append("--schedule-time")
        if missing:
            raise ValueError(f"命令行模式必须显式提供参数：{'、'.join(missing)}")

        book_name = args.book
        book = find_book_queue(book_dirs, book_name)
        if not book:
            available = "、".join(item.name for item in book_dirs)
            raise ValueError(f"未找到待发小说【{book_name}】。当前可选：{available}")
        publish_count = args.count
        volume_num = args.volume
    else:
        book = choose_book_interactively(book_dirs, args.book or DEFAULT_INTERACTIVE_BOOK)
        print(f"\n已选择：【{book.name}】，共 {len(book.txt_files)} 章待发")
        print("==================================================")
        publish_count = args.count if args.count is not None else prompt_publish_count(
            len(book.txt_files), DEFAULT_INTERACTIVE_COUNT
        )
        volume_num = args.volume if args.volume is not None else prompt_volume_num(DEFAULT_INTERACTIVE_VOLUME_NUM)

    if publish_count <= 0:
        raise ValueError("发布章节数量必须大于 0")
    if publish_count > len(book.txt_files):
        print(f"    [提示] 输入数量 {publish_count} 大于待发总数 {len(book.txt_files)}，将发布全部章节。")
        publish_count = len(book.txt_files)
    txt_files = book.txt_files[:publish_count]

    if volume_num is not None and volume_num < 0:
        raise ValueError("卷号不能小于 0")
    if volume_num == 0:
        volume_num = None
    volume_name = cn_volume_name(volume_num)

    current_uploaded_dir = uploaded_dir / book.name
    volume_dir = current_uploaded_dir / volume_name if volume_name else current_uploaded_dir

    schedule_time = None if args.no_schedule else (args.schedule_time or DEFAULT_INTERACTIVE_SCHEDULE_TIME)
    if schedule_time and not re.fullmatch(r"\d{1,2}:\d{2}", schedule_time):
        raise ValueError("定时时间必须使用 HH:MM 格式，例如 18:00")

    return PublishPlan(
        book_name=book.name,
        txt_files=txt_files,
        volume_num=volume_num,
        volume_name=volume_name,
        volume_dir=volume_dir,
        schedule_time=schedule_time,
        ai_used=args.ai_used,
        use_basic_check=not args.full_check,
        dry_run=args.dry_run,
        headless=args.headless,
        non_interactive=non_interactive,
        keep_open=args.keep_open,
    )


def print_plan(plan: PublishPlan) -> None:
    queue = " -> ".join(path.name for path in plan.txt_files)
    print("\n==================================================")
    print("发布计划")
    print("==================================================")
    print(f"小说：{plan.book_name}")
    print(f"章节：前 {len(plan.txt_files)} 章")
    print(f"队列：{queue}")
    print(f"分卷：{plan.volume_name or '不切换分卷'}")
    print(f"AI：{'是' if plan.ai_used == 'yes' else '否'}")
    print(f"检测：{'仅基础检测' if plan.use_basic_check else '全面检测'}")
    print(f"定时：{plan.schedule_time or '立即发布'}")
    print(f"归档：{plan.volume_dir}")
    print("==================================================\n")


def safe_visible(locator) -> bool:
    try:
        return locator.is_visible()
    except Exception:
        return False


def safe_click(locator, timeout: int = 0) -> bool:
    try:
        if timeout:
            locator.wait_for(state="visible", timeout=timeout)
        if locator.is_visible():
            locator.click(force=True)
            return True
    except Exception:
        return False
    return False


def click_named_button(page, names: list[str], timeout: int = 0, last: bool = True) -> bool:
    for name in names:
        pattern = re.compile(rf"^\s*{re.escape(name)}\s*$")
        locators = [
            page.get_by_role("button", name=pattern),
            page.get_by_text(name, exact=True),
        ]
        for locator in locators:
            target = locator.last if last else locator.first
            if safe_click(target, timeout=timeout):
                return True
    return False


def dismiss_guides(page) -> None:
    print(" -> 清理新手引导和遮挡弹窗...")
    for _ in range(3):
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

    for _ in range(10):
        clicked_guide = False
        try:
            for target_text in ["下一步", "完成", "我知道了", "跳过"]:
                btns = page.get_by_text(target_text, exact=True).element_handles()
                for btn in btns:
                    box = btn.bounding_box()
                    if box and box["y"] > 100:
                        print(f"    - 清理引导按钮：{target_text}")
                        btn.click()
                        page.wait_for_timeout(600)
                        clicked_guide = True
        except Exception:
            pass
        if not clicked_guide:
            break


def dismiss_platform_popups(page, wait_ms: int = 500) -> bool:
    page.wait_for_timeout(wait_ms)
    for text in ["我知道了", "知道了", "关闭"]:
        if click_named_button(page, [text], timeout=300):
            page.wait_for_timeout(500)
            return True
    try:
        close_btn = page.locator('button[aria-label="Close"], .semi-modal-close, [class*="close"]').last
        if safe_click(close_btn, timeout=300):
            page.wait_for_timeout(500)
            return True
    except Exception:
        pass
    return False


def parse_chapter(file_path: Path) -> tuple[str, str, str]:
    filename = file_path.name
    raw_title = file_path.stem
    match = re.search(r"第\s*0*(\d+)\s*章[\s_]*(.*)", raw_title)
    chapter_num = str(int(match.group(1))) if match else ""
    chapter_title = match.group(2).strip() if match else ""

    lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
    first_line = lines[0].strip() if lines else ""
    if not chapter_num and first_line:
        first_num = re.search(r"第\s*0*(\d+)\s*章", first_line)
        if first_num:
            chapter_num = str(int(first_num.group(1)))
    if not chapter_title and first_line:
        first_title = re.search(r"第\s*\d+\s*章[\s：:]*(.*)", first_line)
        if first_title:
            chapter_title = first_title.group(1).strip()
    if not chapter_title:
        chapter_title = re.sub(r"^[0-9]+[\s_]*", "", raw_title).strip()

    if lines and re.search(r"第.*?章", lines[0].strip()):
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    content = "".join(lines)
    return chapter_num, chapter_title, content


def open_chapter_manage(page, book_name: str) -> None:
    print(" -> 正在跳转回后台【我的小说】总览...")
    page.goto(BOOK_MANAGE_URL, timeout=60000)
    page.wait_for_timeout(3000)

    print(f" -> 寻找【{book_name}】对应的小说卡片...")
    manage_clicked = False
    book_cards = page.locator("div, li, section, article").filter(has_text=book_name)
    for i in range(book_cards.count() - 1, -1, -1):
        card = book_cards.nth(i)
        try:
            if not card.is_visible():
                continue
            card.hover(timeout=3000)
            page.wait_for_timeout(1000)
            manage_btn = card.get_by_text("章节管理").first
            if manage_btn.is_visible():
                manage_btn.click()
                manage_clicked = True
                break
        except Exception:
            continue

    if not manage_clicked:
        print("    [备选] 卡片 hover 未触发按钮，尝试全局查找【章节管理】...")
        all_cards = page.locator('[class*="book"], [class*="card"], [class*="item"]').filter(has_text=book_name)
        for i in range(all_cards.count()):
            try:
                card = all_cards.nth(i)
                if card.is_visible():
                    card.hover(timeout=2000)
                    page.wait_for_timeout(800)
                    global_btn = page.get_by_text("章节管理").first
                    if global_btn.is_visible():
                        global_btn.click()
                        manage_clicked = True
                        break
            except Exception:
                continue

    if not manage_clicked:
        print("    [警告] 所有 hover 策略失败，退化为直接点击第一个可见的【章节管理】...")
        page.get_by_text("章节管理").first.click()

    page.wait_for_timeout(4000)


def open_or_create_chapter(editor_page, context, chapter_num: str) -> object:
    original_pages = len(context.pages)
    print(f" -> 扫描已有草稿列表，排查是否存在【第 {chapter_num} 章】的历史遗留...")
    draft_row = editor_page.locator("tr, li, .chapter-item").filter(
        has_text=re.compile(rf"第\s*0*{re.escape(chapter_num)}\s*章")
    ).first
    if safe_visible(draft_row):
        print(" -> 发现被中断的草稿历史记录，进入编辑覆盖。")
        edit_icon = draft_row.locator("td").last.locator("svg, i, a, span, button, img").first
        if safe_visible(edit_icon):
            edit_icon.click(force=True)
        else:
            draft_row.click(force=True)
    else:
        print(" -> 确认为全新章节，点击【新建章节】...")
        new_btn = editor_page.get_by_role("button", name="新建章节").first
        if not safe_visible(new_btn):
            new_btn = editor_page.get_by_text("新建章节").first
        new_btn.click(force=True)

    editor_page.wait_for_timeout(4000)
    if len(context.pages) > original_pages:
        return context.pages[-1]
    return editor_page


def select_volume(editor_page, plan: PublishPlan) -> None:
    if plan.volume_num is None:
        print(" -> 按计划不切换分卷。")
        return

    print(f" -> 确认/切换分卷，目标：【{plan.volume_name}】...")
    dialog_opened = False
    vol_elements = editor_page.get_by_text(re.compile(r"第[一二三四五六七八九十百0-9]+卷")).element_handles()
    for element in vol_elements[:10]:
        try:
            box = element.bounding_box()
            if not box or box["y"] < 0 or box["y"] > 900:
                continue
            outer_html = element.evaluate("el => el.outerHTML") or ""
            if "outline" in outer_html.lower() or "placeholder" in outer_html.lower() or "卷名" in outer_html:
                continue
            element.click(force=True)
            editor_page.wait_for_timeout(1000)
            if safe_visible(editor_page.get_by_text("新建分卷").first) or safe_visible(editor_page.get_by_text("取消").first):
                dialog_opened = True
                break
        except Exception:
            continue

    if not dialog_opened:
        if plan.volume_num == 1 and safe_visible(editor_page.get_by_text(plan.volume_name, exact=False).first):
            print(f"    - 页面已显示【{plan.volume_name}】，沿用当前分卷。")
            return
        handle_manual_or_fail(plan, f"未能打开分卷弹窗，无法自动确认【{plan.volume_name}】")
        return

    target_vol = None
    for candidate_name in [plan.volume_name, f"第{plan.volume_num}卷", f"卷{plan.volume_num}"]:
        candidates = editor_page.get_by_text(candidate_name, exact=False).element_handles()
        for candidate in candidates:
            try:
                box = candidate.bounding_box()
                if not box or box["y"] < 0 or box["y"] > 900:
                    continue
                outer_html = candidate.evaluate("el => el.outerHTML") or ""
                if "outline" in outer_html.lower() or "placeholder" in outer_html.lower() or "卷名" in outer_html:
                    continue
                target_vol = candidate
                break
            except Exception:
                continue
        if target_vol:
            break

    if not target_vol:
        handle_manual_or_fail(plan, f"分卷弹窗中未找到【{plan.volume_name}】")
        return

    target_vol.click(force=True)
    editor_page.wait_for_timeout(500)
    if not click_named_button(editor_page, ["确定"], timeout=1000, last=False):
        editor_page.keyboard.press("Escape")
        print("    [警告] 未找到确定按钮，已用 Escape 关闭弹窗。")
    else:
        print(f"    - 已确认分卷：{plan.volume_name}")
    editor_page.wait_for_timeout(1000)


def handle_manual_or_fail(plan: PublishPlan, message: str) -> None:
    if plan.non_interactive:
        raise RuntimeError(message)
    print(f"    [需要人工辅助] {message}")
    prompt_input("    请在浏览器里手动处理后，回到终端按回车继续 >>> ")


def fill_chapter(editor_page, page, chapter_num: str, chapter_title: str, content: str) -> None:
    print(" -> 填入章节序号、标题和正文...")
    num_input = editor_page.locator('input[type="text"]').first
    if safe_visible(num_input):
        num_input.fill(chapter_num, force=True)

    title_input = editor_page.get_by_placeholder("请输入标题", exact=False).first
    if not safe_visible(title_input):
        title_input = editor_page.get_by_placeholder("请输入章节名", exact=False).first
    if not safe_visible(title_input):
        title_input = editor_page.locator('input[type="text"]').last
    if safe_visible(title_input):
        title_input.fill(chapter_title, force=True)

    editor = editor_page.locator(".ql-editor").first
    if not safe_visible(editor):
        editor = editor_page.locator(".ProseMirror").first
    if not safe_visible(editor):
        editor = editor_page.locator('[contenteditable="true"]').first
    if not safe_visible(editor):
        raise RuntimeError("未找到正文输入区域")

    editor.click(force=True)
    editor_page.keyboard.press("Control+A")
    editor_page.keyboard.press("Backspace")
    editor_page.evaluate(
        """([el, text]) => {
            const normalized = text.replace(/\\r\\n/g, "\\n").replace(/\\r/g, "\\n").replace(/\\n+$/g, "");
            const lines = normalized.split("\\n");
            const makeParagraph = (line) => {
                const p = document.createElement("p");
                if (line.length === 0) {
                    p.appendChild(document.createElement("br"));
                } else {
                    p.appendChild(document.createTextNode(line));
                }
                return p;
            };

            el.innerHTML = "";
            for (const line of lines) {
                el.appendChild(makeParagraph(line));
            }

            el.dispatchEvent(new InputEvent("input", {
                bubbles: true,
                inputType: "insertText",
                data: text
            }));
            el.dispatchEvent(new Event("change", {bubbles: true}));

            const quill = el.__quill || (window.Quill && window.Quill.find ? window.Quill.find(el) : null);
            if (quill && typeof quill.update === "function") {
                quill.update("user");
            }
        }""",
        [editor.element_handle(), content],
    )
    editor.click()
    editor_page.keyboard.press("End")
    editor_page.keyboard.press("Space")
    page.wait_for_timeout(500)
    editor_page.keyboard.press("Backspace")


def choose_basic_detection(editor_page, plan: PublishPlan) -> bool:
    if not plan.use_basic_check:
        return False

    try:
        detector_hint = editor_page.get_by_text(re.compile("请选择内容检测方式|基础检测|全面检测")).last
        detector_hint.wait_for(state="visible", timeout=5000)
    except Exception:
        return False

    editor_page.wait_for_timeout(500)
    if click_named_button(editor_page, ["仅基础检测", "基础检测"], timeout=2000):
        print("    - 已选择【仅基础检测】。")
        editor_page.wait_for_timeout(2000)
        return True
    return False


def handle_after_next(editor_page, plan: PublishPlan) -> None:
    for _ in range(3):
        try:
            submit_typo_btn = editor_page.get_by_role("button", name="提交").first
            submit_typo_btn.wait_for(state="visible", timeout=1500)
            print("    - 触发错别字/提示弹窗，点击【提交】继续。")
            submit_typo_btn.click(force=True)
            editor_page.wait_for_timeout(1200)
            continue
        except Exception:
            pass

        if choose_basic_detection(editor_page, plan):
            continue

        break


def click_ai_option(editor_page, ai_used: str) -> None:
    target_text = "是" if ai_used == "yes" else "否"
    print(f"    - 设置【是否使用 AI】：{target_text}")
    for locator in [
        editor_page.get_by_role("radio", name=re.compile(target_text)).last,
        editor_page.get_by_text(target_text, exact=True).last,
    ]:
        if safe_click(locator, timeout=1000):
            editor_page.wait_for_timeout(500)
            return
    print("    [警告] 未能自动点击 AI 选项，继续尝试发布设置。")


def configure_schedule(editor_page, schedule_time: str | None) -> None:
    if not schedule_time:
        return

    print(f"    - 设置定时发布：{schedule_time}")
    try:
        editor_page.get_by_text("定时发布", exact=False).first.wait_for(state="visible", timeout=3000)
    except Exception:
        print("    [警告] 未看到定时发布区域，跳过定时设置。")
        return

    editor_page.evaluate(
        """() => {
            const visible = (el) => {
                const box = el.getBoundingClientRect();
                return box.width > 0 && box.height > 0;
            };
            const switches = Array.from(document.querySelectorAll('[role="switch"], input[type="checkbox"], [class*="switch"]'))
                .filter(visible);
            const labelNodes = Array.from(document.querySelectorAll('body *'))
                .filter(el => visible(el) && el.textContent && el.textContent.trim() === '定时发布');
            const label = labelNodes[labelNodes.length - 1];
            const near = label ? label.closest('section, form, div') : null;
            const candidates = near
                ? switches.filter(el => near.contains(el) || (near.parentElement && near.parentElement.contains(el)))
                : switches;
            const sw = candidates[candidates.length - 1] || switches[switches.length - 1];
            if (!sw) return;
            const aria = sw.getAttribute('aria-checked');
            const checked = sw.checked === true || aria === 'true' || /checked|open|active/i.test(sw.className || '');
            if (!checked) sw.click();
        }"""
    )
    editor_page.wait_for_timeout(800)

    time_input = find_time_input(editor_page)
    if not time_input:
        raise RuntimeError("未找到定时发布时间输入框")

    # Use the same gesture as a human: click the field, select/delete the existing hh:mm,
    # type the desired time, then confirm the time-picker popover if it appears.
    time_input.click(force=True)
    editor_page.wait_for_timeout(200)
    editor_page.keyboard.press("Control+A")
    editor_page.keyboard.press("Backspace")
    editor_page.keyboard.type(schedule_time, delay=40)
    editor_page.wait_for_timeout(500)

    actual_time = (time_input.evaluate("el => el.value") or "").strip()
    if actual_time != schedule_time:
        try:
            time_input.fill(schedule_time, force=True)
            editor_page.wait_for_timeout(500)
            actual_time = (time_input.evaluate("el => el.value") or "").strip()
        except Exception:
            pass

    click_named_button(editor_page, ["确定"], timeout=1000)
    editor_page.wait_for_timeout(500)
    actual_time = (find_time_input(editor_page).evaluate("el => el.value") or "").strip()
    if actual_time != schedule_time:
        raise RuntimeError(f"定时时间设置失败：期望 {schedule_time}，页面当前为 {actual_time or '空'}")


def find_time_input(editor_page):
    inputs = editor_page.locator("input").element_handles()
    visible_inputs = []
    for input_el in inputs:
        try:
            info = input_el.evaluate(
                """el => {
                    const box = el.getBoundingClientRect();
                    return {
                        visible: box.width > 0 && box.height > 0,
                        value: el.value || '',
                        placeholder: el.placeholder || '',
                        type: el.type || ''
                    };
                }"""
            )
        except Exception:
            continue
        if info.get("visible"):
            visible_inputs.append((input_el, info))

    for input_el, info in visible_inputs:
        if re.fullmatch(r"\d{1,2}:\d{2}|[hH]{2}:[mM]{2}", info.get("value", "")):
            return input_el
    for input_el, info in visible_inputs:
        if re.search(r"时间|time|[hH]{2}:[mM]{2}", info.get("placeholder", "")):
            return input_el
    return visible_inputs[-1][0] if visible_inputs else None


def finalize_publish(editor_page, plan: PublishPlan) -> None:
    print("    - 等待【发布设置】面板...")
    publish_btn = editor_page.get_by_role("button", name="确认发布").first
    publish_btn.wait_for(state="visible", timeout=10000)
    click_ai_option(editor_page, plan.ai_used)
    configure_schedule(editor_page, plan.schedule_time)
    publish_btn.click(force=True)


def submit_chapter(editor_page, plan: PublishPlan) -> None:
    print(" -> 点击【下一步】并进入发布设置...")
    next_btn = editor_page.get_by_text("下一步", exact=True).last
    if not safe_visible(next_btn):
        raise RuntimeError("未找到【下一步】按钮")
    next_btn.click(force=True)
    handle_after_next(editor_page, plan)
    finalize_publish(editor_page, plan)


def publish_one_chapter(page, context, plan: PublishPlan, file_path: Path, index: int, total: int) -> bool:
    filename = file_path.name
    chapter_num, chapter_title, content = parse_chapter(file_path)
    print(f"\n[{index}/{total}] 正在处理：第{chapter_num}章「{chapter_title}」 ({filename})")

    open_chapter_manage(page, plan.book_name)
    dismiss_platform_popups(context.pages[-1] if len(context.pages) > 1 else page, wait_ms=1000)

    original_pages = len(context.pages)
    editor_page = context.pages[-1] if original_pages > 1 and context.pages[-1] != page else page
    editor_page = open_or_create_chapter(editor_page, context, chapter_num)
    dismiss_guides(editor_page)
    dismiss_platform_popups(editor_page, wait_ms=500)
    select_volume(editor_page, plan)
    fill_chapter(editor_page, page, chapter_num, chapter_title, content)
    submit_chapter(editor_page, plan)

    page.wait_for_timeout(3000)
    plan.volume_dir.mkdir(parents=True, exist_ok=True)
    dest_path = plan.volume_dir / filename
    shutil.move(str(file_path), str(dest_path))
    print(f"  [发布成功] 已归档：{dest_path}")

    if editor_page != page:
        editor_page.close()
    return True


def run_publish(plan: PublishPlan) -> int:
    print_plan(plan)
    if plan.dry_run:
        print("[dry-run] 已完成命令行/发布计划校验，不启动浏览器，不移动文件。")
        return 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=plan.headless)
        proxy = get_playwright_proxy()
        if proxy:
            print(f" -> 已为 Playwright 显式启用代理：{proxy['server']}")
        context = browser.new_context(storage_state=STATE_FILE, proxy=proxy)
        page = context.new_page()

        success_count = 0
        for index, file_path in enumerate(plan.txt_files, 1):
            try:
                if publish_one_chapter(page, context, plan, file_path, index, len(plan.txt_files)):
                    success_count += 1
            except Exception as exc:
                print(f"!!! 处理 {file_path.name} 时发生错误：{exc}")
                if plan.non_interactive:
                    browser.close()
                    return 1
                keep_going = prompt_input("请在浏览器中查看问题。按回车停止；输入 y 继续下一章 >>> ").strip().lower()
                if keep_going != "y":
                    break
            page.wait_for_timeout(1000)

        print("\n==========================================")
        print(f"发布流程结束。本次成功发送 {success_count}/{len(plan.txt_files)} 个章节。")
        print("==========================================\n")

        if plan.keep_open or not plan.non_interactive:
            prompt_input(">>> 按回车关闭浏览器：")
        browser.close()
        return 0 if success_count == len(plan.txt_files) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="番茄小说章节发布工具。默认交互运行；使用 --cli 可进入非交互命令行模式。"
    )
    parser.add_argument("--cli", action="store_true", help="非交互命令行模式；必须显式提供 book/count/volume/schedule-time")
    parser.add_argument("--book", help="目标小说名，例如：死人请柬")
    parser.add_argument("--count", type=int, help="本次发布前 N 章，例如：每天发两章时填 2")
    parser.add_argument("--volume", type=int, help="目标分卷号；0 表示不切换，例如：1")
    parser.add_argument("--schedule-time", help="定时发布时间，例如：18:00")
    parser.add_argument("--no-schedule", action="store_true", help="不启用定时发布")
    parser.add_argument("--ai-used", choices=["yes", "no"], default="no", help="是否使用 AI，默认 no")
    parser.add_argument("--full-check", action="store_true", help="使用全面检测；默认点击仅基础检测")
    parser.add_argument("--headless", action="store_true", help="无头浏览器模式")
    parser.add_argument("--keep-open", action="store_true", help="发布结束后等待回车再关闭浏览器")
    parser.add_argument("--dry-run", action="store_true", help="只校验队列和参数，不启动浏览器，不移动文件")
    parser.add_argument("--chapters-dir", default=CHAPTERS_DIR, help="待发章节根目录")
    parser.add_argument("--uploaded-dir", default=UPLOADED_DIR, help="发布成功后的归档根目录")
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        plan = build_plan(args)
        return run_publish(plan)
    except (FileNotFoundError, ValueError, RuntimeError, PlaywrightTimeoutError) as exc:
        print(f"[错误] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
