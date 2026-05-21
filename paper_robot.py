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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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
    payload = {"ids": sorted(seen), "updated_at": dt.datetime.now(dt.UTC).isoformat()}
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
        return dt.datetime(1900, 1, 1, tzinfo=dt.UTC)
    return dt.datetime.fromisoformat(value).replace(tzinfo=dt.UTC)


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


def score_work(work: dict, config: dict, query_name: str) -> int:
    title = work.get("display_name") or ""
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    haystack = f"{title} {abstract} {source_name(work)} {publisher_name(work)}"
    score = 0
    score += 8 * keyword_score(haystack, config.get("input_output_keywords", []))
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
        journal = source_name(work)
        paper = {
            "id": work.get("id") or doi_url(work),
            "title": normalize_text(work.get("display_name") or ""),
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


def summarize_paper(paper: dict, config: dict) -> dict:
    sections = structured_sections(paper["abstract"])
    summary_text = " ".join(sections.values()) if sections else paper["abstract"]
    sentences = sentence_split(summary_text)
    methods = config.get("method_keywords", [])
    io_terms = config.get("input_output_keywords", [])
    originality_terms = ["new", "novel", "first", "contribute", "propose", "develop", "introduce"]
    conclusion_terms = ["find", "show", "result", "suggest", "indicate", "reveal"]
    background = section_value(sections, ["background"]) or first_matching_sentence(
        sentences, config.get("field_keywords", []), 0
    )
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
    if "multi-regional input-output" in lowered or "mrio" in lowered:
        return "多区域投入产出表（MRIO）"
    if "input-output" in lowered or "input output" in lowered or "leontief" in lowered:
        return "投入产出表/Leontief 乘数分析"
    if "decomposition" in lowered:
        return "结构分解或因素分解"
    if "difference-in-differences" in lowered:
        return "双重差分"
    if "instrumental variable" in lowered:
        return "工具变量"
    if "panel" in lowered:
        return "面板数据模型"
    if "spatial" in lowered:
        return "空间计量模型"
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


def chinese_summary(paper: dict, config: dict) -> dict:
    raw = summarize_paper(paper, config)
    joined = " ".join(raw.values())
    field = field_label(joined, paper["query_name"])
    method = method_label(raw["method"])
    finding = finding_label(raw["findings"])
    io_focus = "投入产出表、产业关联或供应链传导机制" if method.startswith("投入产出") or "MRIO" in method else "经济机制识别"
    return {
        "background": (
            f"这篇论文处在{field}的研究脉络中，核心问题不是单个部门或单项政策的孤立效果，"
            "而是经济活动如何沿着部门投入、供应链关系和区域结构继续扩散。"
            f"从题目《{paper['title']}》来看，论文特别适合用来观察产业联系、公共政策或区域发展"
            "如何影响就业、产出、增加值、环境压力或城市体系等结果。"
        ),
        "purpose": (
            "研究目的可以概括为：在一个可量化的经济系统中识别研究对象的直接影响和间接影响，"
            "并说明这些影响为什么会通过部门之间的购买、销售、生产配套或空间联系被放大或重新分配。"
            "换句话说，它不只是回答“有没有影响”，还试图回答影响发生在哪些部门、通过什么路径传导、"
            "以及这些结果对政策评估或产业选择意味着什么。"
        ),
        "originality": (
            f"独创性主要在于把问题放到{io_focus}中讨论。相比只看局部样本或单一结果变量，"
            "这种视角能把直接效应、上游供应链效应、下游需求效应和区域间溢出放在同一框架下衡量。"
            "如果论文使用的是投入产出表或 MRIO，它的价值尤其在于能把宏观总量、部门结构和政策含义连接起来，"
            "让读者看到某一部门或地区变化背后的系统性连锁反应。"
        ),
        "method": (
            f"研究手法以{method}为核心，并结合论文摘要中的数据设定估计直接、间接或总体经济效应。"
            "这类方法通常会先定义部门之间的投入和产出关系，再计算需求变化、政策冲击或部门扩张带来的乘数效应。"
            "如果数据允许，还可以进一步拆分就业、收入、增加值、碳排放或区域流向，从而判断哪些部门是关键传导节点，"
            "哪些结果只是表面相关，哪些结果具有更强的结构性解释力。"
        ),
        "findings": (
            f"结论部分显示，{finding}。对研究者来说，这类发现的意义在于它能帮助判断政策收益是否只停留在目标部门，"
            "还是会通过供应链和区域网络产生更广泛的经济后果。对政策制定者来说，它也能提示资源应该投向哪里："
            "是优先支持乘数更高的部门，还是关注就业、环境和区域公平之间的权衡。整体来看，这篇论文更适合作为"
            "理解经济结构传导机制的材料，而不只是作为一个单独案例阅读。"
        ),
    }


def ensure_minimum_summary(description: str, minimum_chars: int) -> str:
    if len(description) >= minimum_chars:
        return description
    supplement = (
        "\n**补充解读**: 从论文筛选角度看，这篇文章之所以值得推送，是因为它与投入产出表、"
        "产业关联或区域经济传导问题存在明确联系。阅读时可以重点关注三个层面：第一，作者如何定义"
        "冲击或研究对象；第二，部门之间的中间投入关系如何改变最终估计结果；第三，论文是否把产出、"
        "就业、环境或空间分布等结果放在同一个经济系统里解释。这些信息有助于判断论文是否能为你后续"
        "做环境、劳动、城市或区域经济研究提供可复用的方法。"
    )
    return description + supplement


def trim(text: str, limit: int) -> str:
    text = normalize_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


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
    return {
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


def post_to_discord(webhook_url: str, payload: dict, dry_run: bool) -> None:
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "economics-paper-robot/0.2"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError(f"Discord webhook failed with status {response.status}")


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
