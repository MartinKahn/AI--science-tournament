#!/usr/bin/env python3
"""Search PubMed reproducibly and save source XML plus deduplicated metadata.

No dependencies or NCBI API key required. --retmax limits results PER QUERY;
the log preserves total PubMed hit counts separately from retrieved counts.
"""
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
DEFAULT_QUERIES = [('example', 'reproducibility[Title/Abstract] AND benchmark[Title/Abstract]')]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def node_text(node):
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def parse_search(xml):
    root = ET.fromstring(xml)
    error = root.findtext("ERROR")
    if error:
        raise ValueError("PubMed ESearch error: " + error)
    return {
        "total_count": int(root.findtext("Count", "0")),
        "pmids": [node.text for node in root.findall("./IdList/Id") if node.text],
        "query_translation": root.findtext("QueryTranslation", ""),
        "warnings": [node_text(n) for n in root.findall("./WarningList/*")],
        "errors": [node_text(n) for n in root.findall("./ErrorList/*")],
    }


def parse_articles(xml):
    """Extract article metadata, retaining structured abstract sections and IDs."""
    root = ET.fromstring(xml)
    error = root.findtext("ERROR")
    if error:
        raise ValueError("PubMed EFetch error: " + error)
    results = []
    for record in root.findall("./PubmedArticle"):
        citation = record.find("MedlineCitation")
        article = citation.find("Article") if citation is not None else None
        if article is None:
            continue
        pmid = node_text(citation.find("PMID"))
        identifiers = {}
        for node in record.findall("./PubmedData/ArticleIdList/ArticleId"):
            identifiers[node.get("IdType", "unknown")] = node_text(node)
        doi = identifiers.get("doi", "")
        if not doi:
            doi = next((node_text(n) for n in article.findall("ELocationID")
                        if n.get("EIdType") == "doi" and n.get("ValidYN") != "N"), "")
        pubdate = article.find("./Journal/JournalIssue/PubDate")
        date_text = " ".join(node_text(n) for n in pubdate) if pubdate is not None else ""
        year = pubdate.findtext("Year", "") if pubdate is not None else ""
        if not year:
            medline_date = pubdate.findtext("MedlineDate", "") if pubdate is not None else ""
            match = re.search(r"\b(?:18|19|20)\d{2}\b", medline_date)
            year = match.group(0) if match else article.findtext("./ArticleDate/Year", "")
        sections = [{"label": n.get("Label", ""), "category": n.get("NlmCategory", ""),
                     "text": node_text(n)} for n in article.findall("./Abstract/AbstractText")]
        pmcid = identifiers.get("pmc", "").upper()
        results.append({
            "pmid": pmid,
            "title": node_text(article.find("ArticleTitle")),
            "abstract": "\n".join((s["label"] + ": " if s["label"] else "") + s["text"] for s in sections),
            "abstract_sections": sections,
            "doi": doi,
            "year": year,
            "publication_date": date_text,
            "journal": node_text(article.find("./Journal/Title")),
            "journal_abbreviation": node_text(article.find("./Journal/ISOAbbreviation")),
            "publication_types": [node_text(n) for n in article.findall("./PublicationTypeList/PublicationType")],
            "authors": [node_text(n.find("CollectiveName")) or " ".join(filter(None, [n.findtext("ForeName"), n.findtext("LastName")]))
                        for n in article.findall("./AuthorList/Author")],
            "article_identifiers": identifiers,
            "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/",
            "pmc_url": "https://pmc.ncbi.nlm.nih.gov/articles/" + pmcid + "/" if re.fullmatch(r"PMC\d+", pmcid) else None,
            "doi_url": "https://doi.org/" + doi if doi else None,
        })
    return results


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


class PubMedClient:
    def __init__(self, out_dir, attempts=3, interval=0.36):
        self.out_dir = Path(out_dir)
        self.attempts = attempts
        self.interval = max(interval, 1 / 3)
        self.last_request = 0.0

    def request(self, endpoint, params):
        """Bounded retries; every attempt (including errors) is timestamped."""
        params = dict(params, tool="abstract_tournament_literature")
        body = urllib.parse.urlencode(params).encode()
        for attempt in range(1, self.attempts + 1):
            delay = self.interval - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()
            entry = {"timestamp_utc": utc_now(), "endpoint": endpoint,
                     "params": params, "attempt": attempt}
            retry_after = 0
            try:
                request = urllib.request.Request(BASE_URL + endpoint, data=body,
                    headers={"User-Agent": "AbstractTournamentLiterature/1.0", "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    content = response.read()
                    entry["status"] = response.status
                return content
            except urllib.error.HTTPError as exc:
                entry["status"] = exc.code
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.attempts:
                    raise RuntimeError(f"PubMed request failed: HTTP {exc.code}") from None
                value = exc.headers.get("Retry-After", "")
                try:
                    retry_after = float(value)
                except ValueError:
                    try:
                        retry_after = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError):
                        retry_after = 0
            except (urllib.error.URLError, TimeoutError) as exc:
                entry["error_type"] = type(exc).__name__
                if attempt == self.attempts:
                    raise RuntimeError("PubMed connection failed after bounded retries") from None
            finally:
                with (self.out_dir / "requests.jsonl").open("a") as handle:
                    handle.write(json.dumps(entry) + "\n")
            time.sleep(min(max(retry_after, 2 ** (attempt - 1)), 30))
        raise RuntimeError("PubMed attempts exhausted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True, help="New directory for this timestamped search snapshot")
    parser.add_argument("--retmax", type=int, default=100, help="Maximum PMIDs per query (1–9999), default 100")
    parser.add_argument("--query", action="append", help="Custom PubMed query; repeatable. Replaces default suite.")
    args = parser.parse_args()
    if not 1 <= args.retmax <= 9999:
        parser.error("--retmax must be between 1 and 9999")
    if args.out_dir.exists() and any(args.out_dir.iterdir()):
        parser.error("--out-dir must be empty; preserve prior searches in their own directories")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    raw = args.out_dir / "xml"
    raw.mkdir()
    queries = [(f"custom_{i + 1:02d}", q) for i, q in enumerate(args.query)] if args.query else DEFAULT_QUERIES
    client = PubMedClient(args.out_dir)
    provenance, searches = {}, []
    write_json(args.out_dir / "manifest.json", {"started_utc": utc_now(), "retmax_per_query": args.retmax,
               "queries": [{"name": name, "query": query} for name, query in queries],
               "endpoint": BASE_URL, "retrieval_order": "PubMed publication date descending",
               "note": "PubMed abstracts and optional PMC links; PMC full text is not downloaded. This suite is a reproducible search, not an exhaustive systematic review."})
    for index, (name, query) in enumerate(queries, start=1):
        timestamp = utc_now()
        xml = client.request("esearch.fcgi", {"db": "pubmed", "term": query,
                 "retmax": args.retmax, "retmode": "xml", "sort": "pub date"})
        xml_file = f"search-{index:02d}-{name}.xml"
        (raw / xml_file).write_bytes(xml)
        search = parse_search(xml)
        search.update({"name": name, "query": query, "timestamp_utc": timestamp,
                       "retrieved_count": len(search["pmids"]), "source_xml": "xml/" + xml_file})
        search["truncated"] = search["total_count"] > search["retrieved_count"]
        searches.append(search)
        write_json(args.out_dir / "queries.json", searches)
        for pmid in search["pmids"]:
            provenance.setdefault(pmid, []).append(name)
        print(f"{name}: {search['total_count']} hits; retained {search['retrieved_count']} PMIDs", flush=True)
    write_json(args.out_dir / "pmids.json", {"unique_pmids": list(provenance), "query_membership": provenance})
    articles = {}
    write_json(args.out_dir / "articles.json", [])
    pmids = list(provenance)
    for start in range(0, len(pmids), 200):
        batch = pmids[start:start + 200]
        xml = client.request("efetch.fcgi", {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"})
        xml_file = f"fetch-{start // 200 + 1:03d}.xml"
        (raw / xml_file).write_bytes(xml)
        for article in parse_articles(xml):
            article["matched_queries"] = provenance.get(article["pmid"], [])
            article["source_xml"] = "xml/" + xml_file
            articles[article["pmid"]] = article
        write_json(args.out_dir / "articles.json", list(articles.values()))
    missing = [pmid for pmid in pmids if pmid not in articles]
    summary = {"completed_utc": utc_now(), "query_count": len(queries), "unique_pmids": len(pmids),
               "parsed_articles": len(articles), "missing_or_unparsed_pmids": missing,
               "truncated_queries": [q["name"] for q in searches if q["truncated"]],
               "queries_with_warnings_or_errors": [q["name"] for q in searches if q["warnings"] or q["errors"]]}
    write_json(args.out_dir / "summary.json", summary)
    print(f"Saved {len(articles)} deduplicated articles and source XML to {args.out_dir}")
    if missing:
        print(f"Warning: {len(missing)} PMID(s) not parsed; inspect summary and source XML.")


if __name__ == "__main__":
    main()
