import asyncio
import json
import random
import re
import click
from playwright.async_api import async_playwright
from dotenv import load_dotenv
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def _human_delay(min_ms: int, max_ms: int) -> None:
    delay = random.uniform(min_ms, max_ms) / 1000
    await asyncio.sleep(delay)


async def _reading_delay(text: str, wpm: int = 200) -> None:
    words = len(text.split())
    seconds = (words / wpm) * 60
    jitter = random.uniform(0.5, 1.5)
    await asyncio.sleep(max(0.5, seconds * jitter))


def _clean_question_text(raw: str) -> str:
    """Strip leading question numbers like '1.', '2.', newlines, and extra whitespace."""
    text = raw.strip()
    text = re.sub(r"^\d+\.\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _build_answer_map(answers: list[dict]) -> dict[str, dict]:
    return {_clean_question_text(q["question"]): q for q in answers}


async def _get_question_text(q_el) -> str:
    for selector in [
        '[data-automation-id="questionTitle"]',
        ".office-form-question-title",
        "span[dir]",
        "label",
    ]:
        el = await q_el.query_selector(selector)
        if el:
            text = (await el.inner_text()).strip()
            if text:
                return _clean_question_text(text)
    full = (await q_el.inner_text()).strip()
    return _clean_question_text(full.split("\n")[0].strip())


async def _fill_text(q_el, answer: str, human: bool, delays: dict) -> None:
    input_el = await q_el.query_selector('input[data-automation-id="textInput"], textarea')
    if input_el:
        if human:
            await _human_delay(*delays["before_click"])
        await input_el.fill(answer)


async def _fill_choice(q_el, answer, human: bool, delays: dict) -> None:
    answers = answer if isinstance(answer, list) else [answer]

    radios = await q_el.query_selector_all('div[data-automation-id="choiceItem"] input[type="radio"]')
    if radios:
        for inp in radios:
            value = await inp.get_attribute("value")
            if value in answers:
                if human:
                    await _human_delay(*delays["before_click"])
                await inp.click()
        return

    checkboxes = await q_el.query_selector_all('div[data-automation-id="choiceItem"] input[type="checkbox"]')
    for inp in checkboxes:
        value = await inp.get_attribute("value")
        if value in answers:
            if human:
                await _human_delay(*delays["before_click"])
            await inp.click()
            if human:
                await _human_delay(*delays["between_clicks"])

    if not radios and not checkboxes:
        logger.warning("No choice inputs found for answer %r", answers)


def _normalize(text: str) -> str:
    """Lowercase and strip all non-alphanumeric characters for fuzzy comparison."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


async def _find_likert_input(q_el, statement: str, value: str):
    """
    Try multiple aria-label formats used by Microsoft Forms for matrix/likert questions.
    Falls back to fuzzy-normalized matching when exact/contains checks fail.
    """
    # 1. Exact aria-label format variants.
    # CSS attribute selectors break if the value contains the same quote char used as delimiter.
    # Strategy: use double-quoted selector by default; switch to single-quoted if label contains
    # double quotes; skip CSS selector entirely if label contains BOTH quote types (rare).
    candidates = [
        f'{statement} Opcja {value}',
        f'{statement} Option {value}',
        f'{statement} {value}',
        f'{statement}, {value}',
    ]
    for label in candidates:
        has_double = '"' in label
        has_single = "'" in label
        try:
            if not has_double:
                el = await q_el.query_selector(f'input[aria-label="{label}"]')
            elif not has_single:
                el = await q_el.query_selector(f"input[aria-label='{label}']")
            else:
                el = None  # both quote types present — fall through to scan
        except Exception:
            el = None
        if el:
            logger.debug("Matched likert via exact label: %r", label)
            return el

    # Gather all inputs once for the remaining strategies
    all_inputs = await q_el.query_selector_all('input[type="radio"], input[type="checkbox"]')
    labels = [(inp, await inp.get_attribute("aria-label") or "") for inp in all_inputs]

    # 2. Contains: aria-label must include both statement and value verbatim
    for inp, label in labels:
        if statement in label and value in label:
            logger.debug("Matched likert via contains: %r", label)
            return inp

    # 3. Fuzzy: normalize statement + value and compare against normalized aria-labels.
    #    This handles minor punctuation/whitespace/encoding differences in the form HTML.
    norm_stmt = _normalize(statement)
    norm_val = _normalize(value)
    for inp, label in labels:
        norm_label = _normalize(label)
        if norm_stmt in norm_label and norm_val in norm_label:
            logger.debug("Matched likert via fuzzy normalize: %r", label)
            return inp

    # 4. Row-based: find the <tr> whose text contains the statement, then match value
    rows = await q_el.query_selector_all("tr")
    for row in rows:
        row_text = await row.inner_text()
        if _normalize(statement) in _normalize(row_text):
            row_inputs = await row.query_selector_all('input[type="radio"], input[type="checkbox"]')
            for inp in row_inputs:
                label = await inp.get_attribute("aria-label") or ""
                if norm_val in _normalize(label):
                    logger.debug("Matched likert via row scan: %r", label)
                    return inp

    # If still not found, log the actual labels to help diagnose
    logger.warning(
        "Likert input not found for %r = %r. Actual aria-labels in question:\n%s",
        statement, value,
        "\n".join(f"  {lbl!r}" for _, lbl in labels[:12])
    )
    return None


async def _fill_likert(q_el, answer: str, human: bool, delays: dict) -> None:
    try:
        parsed = json.loads(answer)
    except json.JSONDecodeError:
        logger.error("Invalid likert answer JSON: %s", answer)
        return

    # Debug: dump aria-labels once per question so mismatches are easy to diagnose
    if logger.isEnabledFor(logging.DEBUG):
        all_inputs = await q_el.query_selector_all('input[type="radio"], input[type="checkbox"]')
        logger.debug("Likert inputs (%d total), first 8 aria-labels:", len(all_inputs))
        for inp in all_inputs[:8]:
            logger.debug("  %r", await inp.get_attribute("aria-label"))

    for statement, value in parsed.items():
        input_el = await _find_likert_input(q_el, statement, value)
        if input_el:
            if human:
                await _human_delay(*delays["before_click"])
            await input_el.click()
            if human:
                await _human_delay(*delays["between_clicks"])
        else:
            logger.warning("Likert input not found for %r = %r", statement, value)


async def _fill_date(q_el, answer: str, human: bool, delays: dict) -> None:
    input_el = await q_el.query_selector('input[type="date"]')
    if input_el:
        if human:
            await _human_delay(*delays["before_click"])
        await input_el.fill(answer)


async def _fill_page_questions(page, answer_map: dict, human: bool, delays: dict) -> int:
    question_els = await page.query_selector_all('div[data-automation-id="questionItem"]')
    logger.info("Found %d question(s) on this page", len(question_els))
    filled = 0

    for q_el in question_els:
        q_text = await _get_question_text(q_el)
        q_data = answer_map.get(q_text)

        if q_data is None:
            logger.warning("No answer found for question: %r", q_text[:80])
            continue

        q_type = q_data["type"]
        answer = q_data["answer"]
        logger.info("Filling [%s]: %s", q_type, q_text[:60])

        if human:
            await _reading_delay(q_text)

        if q_type == "text":
            await _fill_text(q_el, answer, human, delays)
        elif q_type == "choice":
            await _fill_choice(q_el, answer, human, delays)
        elif q_type == "likert":
            await _fill_likert(q_el, answer, human, delays)
        elif q_type == "date":
            await _fill_date(q_el, answer, human, delays)
        else:
            logger.warning("Unknown question type: %s", q_type)

        if human:
            await _human_delay(*delays["between_questions"])

        filled += 1

    return filled


async def _wait_for_page_change(page, old_first_q_text: str, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.4)
        try:
            q_els = await page.query_selector_all('div[data-automation-id="questionItem"]')
            if q_els:
                new_text = await _get_question_text(q_els[0])
                if new_text != old_first_q_text:
                    logger.debug("Page changed: %r → %r", old_first_q_text[:40], new_text[:40])
                    return True
        except Exception:
            pass
    logger.warning("Page did not change within %.1fs — possible validation error on required field", timeout)
    return False


async def _click_next(page, current_first_q_text: str, human: bool, delays: dict) -> bool:
    next_btn = await page.query_selector('button[data-automation-id="nextButton"]')
    if not next_btn:
        return False
    if human:
        await _human_delay(*delays["before_click"])
    logger.info("Clicking Next →")
    await next_btn.click()
    await _wait_for_page_change(page, current_first_q_text)
    return True


async def _click_submit(page, human: bool, delays: dict) -> bool:
    submit_btn = await page.query_selector('button[data-automation-id="submitButton"]')
    if submit_btn:
        if human:
            await _human_delay(*delays["before_click"])
        logger.info("Submitting form...")
        await submit_btn.click()
        await page.wait_for_load_state("networkidle")
        logger.info("Form submitted successfully")
        return True
    logger.error("Submit button not found")
    return False


async def fill_form(
    url: str,
    answers: list[dict],
    submit: bool = False,
    human: bool = False,
    delays: dict = None,
) -> None:
    if delays is None:
        delays = {
            "page_load": (2000, 5000),
            "before_click": (500, 1500),
            "between_clicks": (300, 800),
            "between_questions": (1000, 3000),
        }

    answer_map = _build_answer_map(answers)
    logger.debug("Answer map keys:\n%s", "\n".join(f"  {k!r}" for k in answer_map))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        logger.info("Loading form: %s", url)
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_selector('div[data-automation-id="questionItem"]', timeout=20000)

        if human:
            await _human_delay(*delays["page_load"])

        page_num = 1
        while True:
            logger.info("--- Page %d ---", page_num)

            q_els = await page.query_selector_all('div[data-automation-id="questionItem"]')
            first_q_text = await _get_question_text(q_els[0]) if q_els else ""

            await _fill_page_questions(page, answer_map, human, delays)

            has_next = await page.query_selector('button[data-automation-id="nextButton"]')
            has_submit = await page.query_selector('button[data-automation-id="submitButton"]')

            if has_next:
                await _click_next(page, first_q_text, human, delays)
                page_num += 1
            elif has_submit:
                if submit:
                    await _click_submit(page, human, delays)
                else:
                    logger.info("Last page reached — review in browser before submitting")
                    input("Press Enter to submit (or Ctrl+C to abort)...")
                    await _click_submit(page, human, delays)
                break
            else:
                logger.warning("Neither Next nor Submit button found — stopping")
                input("Press Enter to close browser...")
                break

        await asyncio.sleep(3)
        await browser.close()


@click.command()
@click.option(
    "--answers", "-a",
    "answers_file",
    default="answers.json",
    show_default=True,
    help="Path to the answers JSON file",
)
@click.option("--submit", is_flag=True, default=False, help="Auto-submit after filling")
@click.option("--human", is_flag=True, default=False, help="Simulate human timing")
@click.option("--min-delay", default=500, help="Min delay between actions in ms")
@click.option("--max-delay", default=4000, help="Max delay between actions in ms")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Debug logging (dumps aria-labels etc.)")
def main(
    answers_file: str,
    submit: bool,
    human: bool,
    min_delay: int,
    max_delay: int,
    verbose: bool,
):
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    load_dotenv()
    url = os.getenv("FORM_URL")
    if not url:
        raise click.UsageError("FORM_URL not set in .env")

    with open(answers_file, encoding="utf-8") as f:
        answers = json.load(f)

    delays = {
        "page_load": (2000, 5000),
        "before_click": (min_delay, min_delay + 1000),
        "between_clicks": (800, 1500),
        "between_questions": (min_delay, max_delay),
    }

    asyncio.run(fill_form(url, answers, submit=submit, human=human, delays=delays))


if __name__ == "__main__":
    main()
