#!/usr/bin/env python3
"""Find economics papers and push one structured recommendation to Discord."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request

try:
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False


OPENALEX_API = "https://api.openalex.org/works"
DEFAULT_SEEN_PATH = pathlib.Path("data/seen.json")


def load_dotenv(path: pathlib.Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        k = key.strip()
        v = value.strip().strip('"').strip("'")
        # Always override if the env var is unset or empty (handles Claude Code's empty ANTHROPIC_API_KEY)
        if not os.environ.get(k):
            os.environ[k] = v


def read_json(path: pathlib.Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_seen(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    data = read_json(path)
    return set(data.get("ids", []))


def save_seen(path: pathlib.Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ids": sorted(seen), "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_json(url: str, timeout: int = 30) -> dict:
    mailto = os.environ.get("OPENALEX_MAILTO", "")
    headers = {"User-Agent": "economics-paper-robot/0.2"}
    if mailto:
        headers["User-Agent"] += f" (mailto:{mailto})"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def reconstruct_abstract(index: dict | None) -> str:
    if not index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, offsets in index.items():
        for offset in offsets:
            positions.append((offset, word))
    return " ".join(word for _, word in sorted(positions))


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_date(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime(1900, 1, 1, tzinfo=dt.timezone.utc)
    return dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc)


def source_name(work: dict) -> str:
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    return source.get("display_name") or "Unknown journal"


def publisher_name(work: dict) -> str:
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    return source.get("publisher") or "Unknown publisher"


def doi_url(work: dict) -> str:
    doi = work.get("doi")
    if doi:
        return doi
    return work.get("id") or ""


def authors_of(work: dict) -> list[str]:
    authorships = work.get("authorships") or []
    names = []
    for authorship in authorships:
        author = authorship.get("author") or {}
        if author.get("display_name"):
            names.append(author["display_name"])
    return names


def grade_for_journal(journal: str, tiers: dict) -> str:
    lowered = journal.casefold()
    for name, grade in tiers.items():
        if name.casefold() == lowered:
            return grade
    for name, grade in tiers.items():
        if name.casefold() in lowered:
            return grade
    return "Unrated in local config"


def keyword_score(text: str, keywords: list[str]) -> int:
    lowered = text.casefold()
    return sum(1 for keyword in keywords if keyword.casefold() in lowered)


def has_strict_input_output_signal(text: str, keywords: list[str]) -> bool:
    lowered = text.casefold()
    for keyword in keywords:
        keyword_lower = keyword.casefold()
        if keyword_lower in {"mrio", "leontief"}:
            if re.search(rf"\b{re.escape(keyword_lower)}\b", lowered):
                return True
            continue
        if keyword_lower in lowered:
            return True
    return False


def score_work(work: dict, config: dict, query_name: str) -> int:
    title = work.get("display_name") or ""
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    haystack = f"{title} {abstract} {source_name(work)} {publisher_name(work)}"
    score = 0
    score += 9 * keyword_score(haystack, config.get("econometric_keywords", []))
    score += 2 * keyword_score(haystack, config.get("input_output_keywords", []))
    score += 3 * keyword_score(haystack, config.get("core_keywords", []))
    score += 2 * keyword_score(haystack, config.get("field_keywords", []))
    score += 1 if query_name.casefold() in haystack.casefold() else 0
    cited_by = int(work.get("cited_by_count") or 0)
    score += min(cited_by // 25, 4)
    return score


def build_openalex_url(query: dict, config: dict) -> str:
    since = dt.date.today() - dt.timedelta(days=int(config.get("fresh_days", 365)))
    filters = [
        "type:article",
        f"from_publication_date:{since.isoformat()}",
    ]
    params = {
        "search": query["search"],
        "filter": ",".join(filters),
        "sort": "publication_date:desc",
        "per-page": int(query.get("max_results", 25)),
    }
    mailto = os.environ.get("OPENALEX_MAILTO", "")
    if mailto:
        params["mailto"] = mailto
    return f"{OPENALEX_API}?{urllib.parse.urlencode(params)}"


def fetch_openalex_papers(query: dict, config: dict) -> list[dict]:
    data = fetch_json(build_openalex_url(query, config))
    papers = []
    for work in data.get("results", []):
        if not work.get("display_name"):
            continue
        abstract = normalize_text(reconstruct_abstract(work.get("abstract_inverted_index")))
        if len(abstract) < int(config.get("minimum_abstract_length", 250)):
            continue
        title = normalize_text(work.get("display_name") or "")
        if config.get("require_input_output_keyword", True):
            io_haystack = f"{title} {abstract}"
            if not has_strict_input_output_signal(io_haystack, config.get("strict_input_output_keywords", [])):
                continue
        journal = source_name(work)
        paper = {
            "id": work.get("id") or doi_url(work),
            "title": title,
            "abstract": abstract,
            "url": doi_url(work),
            "authors": authors_of(work),
            "published": parse_date(work.get("publication_date")),
            "journal": journal,
            "publisher": publisher_name(work),
            "grade": grade_for_journal(journal, config.get("journal_tiers", {})),
            "query_name": query.get("name", "Economics"),
            "score": score_work(work, config, query.get("name", "")),
        }
        papers.append(paper)
    return papers


def sentence_split(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?])\s+", text)
    return [piece.strip() for piece in pieces if len(piece.strip()) > 30]


def structured_sections(text: str) -> dict[str, str]:
    pattern = re.compile(
        r"\b(Background|Objective|Objectives|Purpose|Methods|Methodology|Results|Findings|Conclusion|Conclusions)"
        r"\s*:\s*",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        key = match.group(1).casefold()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[key] = normalize_text(text[start:end])
    return sections


def section_value(sections: dict[str, str], keys: list[str]) -> str:
    for key in keys:
        value = sections.get(key)
        if value:
            return value
    return ""


def first_matching_sentence(sentences: list[str], keywords: list[str], fallback_index: int) -> str:
    lowered_keywords = [keyword.casefold() for keyword in keywords]
    for sentence in sentences:
        lowered = sentence.casefold()
        if any(keyword in lowered for keyword in lowered_keywords):
            return sentence
    if not sentences:
        return "Abstract unavailable."
    return sentences[min(fallback_index, len(sentences) - 1)]


def first_context_sentence(sentences: list[str]) -> str:
    purpose_markers = ["aim", "objective", "purpose", "this paper", "this study", "we examine", "we estimate"]
    method_markers = ["method", "using", "apply", "model", "regression", "input-output"]
    for sentence in sentences:
        lowered = sentence.casefold()
        if any(marker in lowered for marker in purpose_markers + method_markers):
            continue
        return sentence
    return sentences[0] if sentences else "Abstract unavailable."


def summarize_paper(paper: dict, config: dict) -> dict:
    sections = structured_sections(paper["abstract"])
    summary_text = " ".join(sections.values()) if sections else paper["abstract"]
    sentences = sentence_split(summary_text)
    methods = config.get("method_keywords", [])
    io_terms = config.get("input_output_keywords", [])
    originality_terms = ["new", "novel", "first", "contribute", "propose", "develop", "introduce"]
    conclusion_terms = ["find", "show", "result", "suggest", "indicate", "reveal"]
    background = section_value(sections, ["background"]) or first_context_sentence(sentences)
    purpose = section_value(sections, ["objective", "objectives", "purpose"]) or first_matching_sentence(
        sentences, ["aim", "study", "examine", "assess", "investigate", "objective"], 1
    )
    method = section_value(sections, ["methods", "methodology"]) or first_matching_sentence(
        sentences, methods + io_terms, 3
    )
    findings = section_value(sections, ["results", "findings", "conclusion", "conclusions"]) or first_matching_sentence(
        sentences, conclusion_terms, len(sentences) - 1
    )
    originality = first_matching_sentence(sentences, originality_terms + io_terms, 2)
    if purpose == background or purpose in background:
        purpose = f"This paper examines {paper['title']}."
    if originality in {background, purpose}:
        originality = (
            "The contribution is most likely its economics framing around input-output linkages, "
            "sectoral spillovers, and policy-relevant multiplier evidence."
        )
    return {
        "background": background,
        "purpose": purpose,
        "originality": originality,
        "method": method,
        "findings": findings,
    }


def chinese_topic(topic: str) -> str:
    mapping = {
        "Environment + Input-Output": "环境经济 + 投入产出",
        "Labor + Input-Output": "劳动经济 + 投入产出",
        "Urban + Input-Output": "城市/区域经济 + 投入产出",
    }
    return mapping.get(topic, topic)


def chinese_grade(grade: str) -> str:
    if grade == "Unrated in local config":
        return "本地期刊等级表暂未标注"
    return grade


def chinese_publisher(publisher: str) -> str:
    if publisher == "Unknown publisher":
        return "元数据未标明"
    return publisher


def method_label(text: str) -> str:
    lowered = text.casefold()
    if "spatial durbin" in lowered:
        return "空间杜宾模型"
    if "spatial lag" in lowered:
        return "空间滞后模型"
    if "spatial error" in lowered:
        return "空间误差模型"
    if "spatial econometric" in lowered or "spatial econometrics" in lowered:
        return "空间计量模型"
    if "difference-in-differences" in lowered or re.search(r"\bdid\b", lowered):
        return "双重差分"
    if "instrumental variable" in lowered or re.search(r"\biv\b", lowered):
        return "工具变量"
    if "fixed effects" in lowered:
        return "固定效应面板模型"
    if "panel" in lowered:
        return "面板数据模型"
    if "regression discontinuity" in lowered:
        return "断点回归"
    if "synthetic control" in lowered:
        return "合成控制法"
    if "multi-regional input-output" in lowered or "mrio" in lowered:
        return "多区域投入产出表（MRIO）"
    if "input-output" in lowered or "input output" in lowered or "leontief" in lowered:
        return "投入产出表/Leontief 乘数分析"
    if "decomposition" in lowered:
        return "结构分解或因素分解"
    return "摘要中的实证或模型方法"


def field_label(text: str, topic: str) -> str:
    lowered = text.casefold()
    if "environment" in lowered or "emission" in lowered or "carbon" in lowered or "climate" in lowered:
        return "环境经济学"
    if "labor" in lowered or "labour" in lowered or "employment" in lowered or "wage" in lowered:
        return "劳动经济学"
    if "urban" in lowered or "city" in lowered or "regional" in lowered or "housing" in lowered:
        return "城市与区域经济学"
    return chinese_topic(topic)


def finding_label(text: str) -> str:
    lowered = text.casefold()
    effects = []
    if "employment" in lowered or "job" in lowered or "labor" in lowered or "labour" in lowered:
        effects.append("就业与劳动市场")
    if "emission" in lowered or "carbon" in lowered or "environment" in lowered or "climate" in lowered:
        effects.append("环境与排放")
    if "output" in lowered or "multiplier" in lowered or "value-added" in lowered or "supply chain" in lowered:
        effects.append("产出、增加值和供应链外溢")
    if "urban" in lowered or "regional" in lowered or "city" in lowered:
        effects.append("城市或区域经济")
    if not effects:
        return "论文报告了与研究对象相关的经济影响，并给出相应政策含义"
    return "论文重点报告了" + "、".join(effects) + "方面的影响"


def concise_evidence(text: str, limit: int = 220) -> str:
    text = normalize_text(text)
    if not text or text == "Abstract unavailable.":
        return "摘要没有提供足够细节，需要进一步查看全文确认。"
    return trim(text, limit)


def research_object(title: str) -> str:
    title = normalize_text(title)
    match = re.search(r"effects? of (.+?)(?: using |:|$)", title, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"assessment of (.+?)(?: using |:|$)", title, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return title


def region_label(text: str) -> str:
    regions = [
        "ASEAN",
        "China",
        "Japan",
        "Korea",
        "Indonesia",
        "Malaysia",
        "Philippines",
        "Thailand",
        "Vietnam",
        "India",
        "United States",
        "U.S.",
        "EU",
        "European Union",
        "Europe",
        "Poland",
        "Serbia",
        "Brazil",
        "Africa",
        "Latin America",
        "OECD",
    ]
    lowered = text.casefold()
    for region in regions:
        if region.casefold() in lowered:
            return region
    return "论文所覆盖的地区或样本国家"


def object_label(title: str) -> str:
    title = normalize_text(title)
    patterns = [
        r"impact of (.+?)(?: via | using | in | on |:|$)",
        r"effects? of (.+?) in ",
        r"effects? of (.+?) on ",
        r"role of (.+?) in ",
        r"assessment of (.+?) in ",
        r"analysis of (.+?) in ",
        r"forecasting in (.+?) using ",
    ]
    for pattern in patterns:
        match = re.search(pattern, title, flags=re.IGNORECASE)
        if match:
            return normalize_text(match.group(1))
    if ":" in title:
        subtitle = title.split(":", 1)[1]
        return normalize_text(re.sub(r"^(quantifying|assessing|analyzing|measuring)\s+", "", subtitle, flags=re.IGNORECASE))
    obj = research_object(title)
    return obj


_SUMMARY_PROMPT = """\
你是一位经济学文献助理，专门面向环境经济、劳动经济、城市与区域经济方向的研究者。
请根据下方论文信息，用中文写一篇结构化总结。总结要基于摘要的实际内容，不能套用通用模板。

《论文标题》
{title}

《英文摘要》
{abstract}

请严格按以下五个字段输出，每个字段一段，不要输出字段名以外的任何多余标题或说明：

研究背景：先说明研究对象是什么（具体是哪个国家/地区、哪个经济现象或政策问题），再说明要研究的核心问题是什么，然后解释这个问题为什么有研究必要性（现实重要性或政策意义），最后说明先行研究已经做了哪些相关工作、还存在哪些不足或空白——这部分必须来自摘要的实际信息，不能用通用套话代替。

研究目的：从摘要中提炼具体的研究目标，说清楚作者想回答什么问题、针对什么对象、衡量什么结果。

独创性：基于摘要内容，说明本文相对先行研究的主要贡献，例如新数据、新方法、新研究对象、更强的识别策略，或填补了某个先前未被研究的空白。

研究手法：具体说明使用了哪种计量或分析方法（例如空间计量、DID、IV、面板固定效应、投入产出分析等），以及数据来源和样本范围（如果摘要有提及）。

主要结论：用具体数字或具体方向性结论总结核心发现，不要用泛泛的“研究表明有显著影响”代替。如果摘要本身没有具体数字，提炼结论的方向和政策含义。
"""


def _claude_summary(title: str, abstract: str) -> dict | None:
    """Call Claude API to generate a structured Chinese summary. Returns None on failure."""
    if not _ANTHROPIC_AVAILABLE:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        client = _anthropic.Anthropic(api_key=api_key)
        prompt = _SUMMARY_PROMPT.replace("{title}", title).replace("{abstract}", abstract)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        return _parse_claude_summary(raw)
    except Exception as e:
        print(f"[WARN] Claude summary failed ({type(e).__name__}): {e}", file=sys.stderr)
        return None


def _parse_claude_summary(text: str) -> dict:
    """Parse Claude five-section output into a dict."""
    keys = {
        "研究背景": "background",
        "研究目的": "purpose",
        "独创性": "originality",
        "研究手法": "method",
        "主要结论": "findings",
    }
    result = {v: "" for v in keys.values()}
    current_key = None
    buffer = []

    for line in text.splitlines():
        stripped = line.strip()
        matched = False
        for label, field in keys.items():
            if stripped.startswith(label + "：") or stripped.startswith(label + ":"):
                if current_key and buffer:
                    result[current_key] = " ".join(buffer).strip()
                current_key = field
                after_colon = re.split(r"[：:]", stripped, maxsplit=1)[1].strip()
                buffer = [after_colon] if after_colon else []
                matched = True
                break
        if not matched and current_key and stripped:
            buffer.append(stripped)

    if current_key and buffer:
        result[current_key] = " ".join(buffer).strip()

    return result


def chinese_summary(paper: dict, config: dict) -> dict:
    # Try Claude first
    claude_result = _claude_summary(paper["title"], paper["abstract"])
    if claude_result and all(claude_result.values()):
        return claude_result

    # Fallback: rule-based templates
    raw = summarize_paper(paper, config)
    joined = f"{paper['title']} {paper['abstract']} {' '.join(raw.values())}"
    field = field_label(joined, paper["query_name"])
    method = method_label(raw["method"])
    finding = finding_label(raw["findings"])
    obj = object_label(paper["title"])
    region = region_label(joined)
    return {
        "background": (
            f"这篇论文的研究对象是{region}的「{obj}」，属于{field}领域。"
            f"核心问题是{obj}对经济结构、就业、环境或区域发展的影响。"
            "先行研究对相关议题已有讨论，但在因果识别和间接效应刷画上仍有不足。"
        ),
        "purpose": f"以{region}为对象，评估「{obj}」在经济系统中的作用及其影响渠道。",
        "originality": f"相对先行研究，本文在计量识别或研究对象上有所创新。",
        "method": f"研究手法以{method}为核心。",
        "findings": f"结论部分显示，{finding}。",
    }

def ensure_minimum_summary(description: str, minimum_chars: int) -> str:
    return description


def trim(text: str, limit: int) -> str:
    text = normalize_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def forum_thread_name(title: str) -> str:
    cleaned = re.sub(r"[\r\n\t]+", " ", title)
    cleaned = re.sub(r"[@#:`*_~|<>\\[\\]{}]+", "", cleaned)
    cleaned = normalize_text(cleaned)
    return trim(cleaned or "Economics paper", 90)


def forum_tag_ids() -> list[str]:
    raw = os.environ.get("DISCORD_FORUM_TAG_IDS", "")
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def format_authors(authors: list[str]) -> str:
    if not authors:
        return "未知"
    value = ", ".join(authors[:4])
    if len(authors) > 4:
        value += " et al."
    return value


def discord_payload(paper: dict, config: dict) -> dict:
    summary = chinese_summary(paper, config)
    published = paper["published"].strftime("%Y-%m-%d")
    description = (
        f"**研究背景**: {summary['background']}\n"
        f"**研究目的**: {summary['purpose']}\n"
        f"**独创性**: {summary['originality']}\n"
        f"**研究手法**: {summary['method']}\n"
        f"**结论总结**: {summary['findings']}"
    )
    description = ensure_minimum_summary(description, int(config.get("minimum_summary_chars", 400)))
    payload = {
        "embeds": [
            {
                "title": trim(paper["title"], 250),
                "url": paper["url"],
                "description": trim(description, 3900),
                "color": 2067276,
                "fields": [
                    {"name": "领域", "value": chinese_topic(paper["query_name"]), "inline": True},
                    {"name": "发表日期", "value": published, "inline": True},
                    {"name": "期刊等级", "value": trim(chinese_grade(paper["grade"]), 120), "inline": True},
                    {"name": "期刊", "value": trim(paper["journal"], 250), "inline": True},
                    {"name": "出版商/平台", "value": trim(chinese_publisher(paper["publisher"]), 250), "inline": True},
                    {"name": "作者", "value": trim(format_authors(paper["authors"]), 250), "inline": False},
                ],
                "footer": {"text": "元数据来自 OpenAlex；期刊等级来自本地配置。"},
            }
        ]
    }
    if os.environ.get("DISCORD_FORUM_POSTS", "").casefold() in {"1", "true", "yes", "on"}:
        payload["thread_name"] = forum_thread_name(paper["title"])
        tags = forum_tag_ids()
        if tags:
            payload["applied_tags"] = tags
    return payload


def post_to_discord(webhook_url: str, payload: dict, dry_run: bool) -> None:
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    import subprocess
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "-X", "POST", webhook_url,
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=30,
    )
    status = result.stdout.strip()
    if status not in ("200", "204"):
        raise RuntimeError(f"Discord webhook failed with status {status}: {result.stderr}")


def collect_new_papers(config: dict, seen: set[str]) -> list[dict]:
    papers: list[dict] = []
    for query in config.get("queries", []):
        for paper in fetch_openalex_papers(query, config):
            if paper["id"] in seen:
                continue
            papers.append(paper)
        time.sleep(float(config.get("request_pause_seconds", 1)))
    papers.sort(key=lambda paper: (paper["score"], paper["published"]), reverse=True)
    return papers[: int(config.get("max_posts_per_run", 1))]


def main() -> int:
    parser = argparse.ArgumentParser(description="Push one economics paper to Discord.")
    parser.add_argument("--config", default="config.json", help="Path to config JSON.")
    parser.add_argument("--seen", default=str(DEFAULT_SEEN_PATH), help="Path to seen-paper state.")
    parser.add_argument("--env", default=".env", help="Path to .env file.")
    parser.add_argument("--dry-run", action="store_true", help="Print Discord payloads instead of posting.")
    args = parser.parse_args()

    load_dotenv(pathlib.Path(args.env))
    config = read_json(pathlib.Path(args.config))
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url and not args.dry_run:
        print("DISCORD_WEBHOOK_URL is required unless --dry-run is used.", file=sys.stderr)
        return 2

    seen_path = pathlib.Path(args.seen)
    seen = load_seen(seen_path)
    papers = collect_new_papers(config, seen)

    if not papers:
        print("No new papers found.")
        return 0

    for paper in papers:
        post_to_discord(webhook_url, discord_payload(paper, config), args.dry_run)
        seen.add(paper["id"])

    if not args.dry_run:
        save_seen(seen_path, seen)
    print(f"Processed {len(papers)} new paper(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
