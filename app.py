import copy
import html
import io
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
import streamlit as st
from pypdf import PdfReader
from rapidfuzz import fuzz, process

st.set_page_config(page_title="باحث PBL", page_icon="🔎", layout="wide")

# =====================================================================
# Constants
# =====================================================================
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/"
MEDLINE = "https://wsearch.nlm.nih.gov/ws/query"
HEADERS = {"User-Agent": "PBL-Searcher/1.0 (student project)"}
TIMEOUT = 20

# Source key -> label shown on screen (also the display order)
SOURCES = {
    "own": "📄 مصادري",
    "pmc": "📚 PMC (نص كامل)",
    "epmc": "🇪🇺 Europe PMC (نص كامل)",
    "pubmed": "🌐 PubMed (ملخصات)",
    "medline": "🩺 MedlinePlus",
}

# Section filter: label shown on screen -> keywords matched in section titles
SECTION_KEYWORDS = {
    "كل الأقسام": [],
    "المقدمة": ["introduction", "background"],
    "الطرق": ["method", "material"],
    "النتائج": ["result"],
    "المناقشة": ["discussion"],
    "الخلاصة": ["conclusion"],
}

STOPWORDS = {
    "the", "and", "for", "with", "what", "are", "how", "does", "was",
    "were", "this", "that", "from", "about", "into", "its", "can",
}

CSS = """
<style>
mark {background:#FFE08A; padding:0 2px; border-radius:3px;}
.badge {display:inline-block; background:#0F766E; color:#fff;
        padding:2px 10px; border-radius:12px; font-size:0.8rem;}
.ctitle {font-weight:700; margin:6px 0; line-height:1.5;}
.passage {line-height:1.9; background:#F1F8F7; color:#12303A;
          border-radius:8px; padding:8px 12px; margin:4px 0;}
.viewer {max-height:420px; overflow:auto; white-space:pre-wrap;
         line-height:1.8; background:#F1F8F7; color:#12303A;
         border-radius:8px; padding:10px 12px;}
</style>
"""


# =====================================================================
# Text helpers
# =====================================================================
def clean(text):
    """Collapse whitespace."""
    return re.sub(r"\s+", " ", text or "").strip()


def clip(text, n=1200):
    text = clean(text)
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0] + "…"


def get_terms(query):
    """Split the query into useful lowercase words."""
    words = [w for w in re.findall(r"\w+", query.lower())
             if len(w) > 2 and w not in STOPWORDS]
    return words or [query.lower().strip()]


def is_arabic(text):
    return bool(re.search(r"[\u0600-\u06FF]", text))


def highlight(text, terms):
    """Escape the text and wrap the matched terms in <mark>."""
    safe = html.escape(text or "")
    terms = sorted({t for t in terms if t}, key=len, reverse=True)
    if not terms:
        return safe
    pattern = "|".join(re.escape(html.escape(t)) for t in terms)
    return re.sub(pattern, lambda m: f"<mark>{m.group(0)}</mark>",
                  safe, flags=re.IGNORECASE)


def split_sentences(text):
    return [s for s in re.split(r"(?<=[.!?؟])\s+", clean(text)) if s]


def best_sentences(text, terms, n=3, fallback=2):
    """Sentences containing the terms, or the first few as a fallback."""
    sentences = split_sentences(text)
    hits = [s for s in sentences if any(t in s.lower() for t in terms)]
    return hits[:n] or sentences[:fallback]


# =====================================================================
# Own sources (PDF / TXT)
# =====================================================================
def read_pdf(data):
    reader = PdfReader(io.BytesIO(data))
    return [[i, page.extract_text() or ""]
            for i, page in enumerate(reader.pages, start=1)]


def read_txt(data):
    text = None
    for enc in ("utf-8-sig", "cp1256"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = data.decode("utf-8", errors="ignore")
    pages, buf, size = [], [], 0
    for line in text.splitlines():
        buf.append(line)
        size += len(line) + 1
        if size >= 3000:
            pages.append("\n".join(buf))
            buf, size = [], 0
    if buf:
        pages.append("\n".join(buf))
    return [[i, t] for i, t in enumerate(pages or [""], start=1)]


def parse_file(name, data):
    kind = "pdf" if name.lower().endswith(".pdf") else "txt"
    pages = read_pdf(data) if kind == "pdf" else read_txt(data)
    return kind, pages


@st.cache_data(show_spinner=False)
def load_sample_file(path):
    with open(path, "rb") as f:
        data = f.read()
    kind, pages = parse_file(os.path.basename(path), data)
    return kind, pages, data


def chunk_text(text):
    """Split a page into small chunks (about 2 sentences each)."""
    chunks, buf = [], ""
    for s in split_sentences(text):
        buf = f"{buf} {s}".strip()
        if len(buf) >= 120:
            chunks.append(buf)
            buf = ""
    if buf:
        chunks.append(buf)
    return chunks


def match_chunk(chunk, terms):
    """Return (matched words, exact count). Tolerates small typos."""
    low = chunk.lower()
    words = None
    matched, exact = [], 0
    for t in terms:
        if t in low:
            matched.append(t)
            exact += 1
        elif len(t) >= 5:
            if words is None:
                words = list(set(re.findall(r"\w+", low)))
            best = process.extractOne(t, words, scorer=fuzz.ratio,
                                      score_cutoff=78)
            if best:
                matched.append(best[0])
    return matched, exact


def search_own(library, query, limit=10):
    terms = get_terms(query)
    need = max(1, math.ceil(0.5 * len(terms)))
    found = []
    for name, doc in library.items():
        for page, text in doc["pages"]:
            for chunk in chunk_text(text):
                matched, exact = match_chunk(chunk, terms)
                if len(matched) >= need:
                    found.append((len(matched) * 10 + exact, name,
                                  doc["kind"], page, chunk, matched, text))
    found.sort(key=lambda x: x[0], reverse=True)
    cards = []
    for _, name, kind, page, chunk, matched, text in found[:limit]:
        label = f"صفحة {page}" if kind == "pdf" else f"جزء {page}"
        cards.append({
            "source": "own", "uid": "", "title": name, "link": "",
            "meta": "", "passages": [[label, chunk]],
            "terms": matched + terms,
            "extra": clip(text, 6000), "extra_label": "عرض الصفحة كاملة",
            "cite": f"{name}، {label}",
        })
    return cards


# =====================================================================
# Online sources
# =====================================================================
def http_get(url, params=None, timeout=TIMEOUT):
    r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def espell(query):
    """PubMed spelling suggestion ('did you mean')."""
    r = http_get(EUTILS + "espell.fcgi",
                 {"db": "pubmed", "term": query, "tool": "pbl_searcher"}, 10)
    root = ET.fromstring(r.content)
    corrected = clean(root.findtext("CorrectedQuery"))
    if corrected and corrected.lower() != query.lower():
        return corrected
    return None


def search_pubmed(query, terms, limit=5):
    r = http_get(EUTILS + "esearch.fcgi",
                 {"db": "pubmed", "term": query, "retmax": limit,
                  "retmode": "json", "sort": "relevance",
                  "tool": "pbl_searcher"})
    ids = r.json()["esearchresult"]["idlist"]
    if not ids:
        return []
    r = http_get(EUTILS + "efetch.fcgi",
                 {"db": "pubmed", "id": ",".join(ids), "retmode": "xml",
                  "tool": "pbl_searcher"})
    root = ET.fromstring(r.content)
    cards = []
    for art in root.findall(".//PubmedArticle"):
        pmid = art.findtext(".//PMID") or ""
        title_el = art.find(".//ArticleTitle")
        title = clean("".join(title_el.itertext())) if title_el is not None else ""
        parts = []
        for ab in art.findall(".//Abstract/AbstractText"):
            label = ab.get("Label")
            text = clean("".join(ab.itertext()))
            parts.append(f"{label}: {text}" if label else text)
        full = " ".join(parts)
        author = art.findtext(".//AuthorList/Author/LastName") or ""
        journal = (art.findtext(".//Journal/ISOAbbreviation")
                   or art.findtext(".//Journal/Title") or "")
        year = (art.findtext(".//JournalIssue/PubDate/Year")
                or (art.findtext(".//JournalIssue/PubDate/MedlineDate") or "")[:4])
        sents = best_sentences(full, terms, n=3, fallback=2) if full else []
        cards.append({
            "source": "pubmed", "uid": f"PMID{pmid}", "title": title,
            "link": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "meta": " · ".join(x for x in [journal, year] if x),
            "passages": [["ملخص", s] for s in sents],
            "terms": terms,
            "extra": full or "لا يوجد ملخص لهذا البحث",
            "extra_label": "الملخص كامل",
            "cite": f"{author} et al. {title}. {journal}. {year}. PMID {pmid}",
        })
    return cards


def jats_passages(art, terms, section_kw, top=3):
    """Best matching paragraphs from a JATS full-text article."""
    body = art.find("body")
    if body is None:
        return []
    scored = []

    def consider(sec_title, p):
        text = clean("".join(p.itertext()))
        if len(text) < 60:
            return
        low = text.lower()
        distinct = sum(1 for t in terms if t in low)
        if distinct == 0:
            return
        total = sum(low.count(t) for t in terms)
        scored.append((distinct * 100 + total, sec_title, text))

    for sec in body.findall("sec"):
        sec_title = clean(sec.findtext("title"))
        if section_kw and not any(k in sec_title.lower() for k in section_kw):
            continue
        for p in sec.iter("p"):
            consider(sec_title, p)
    if not section_kw:
        for p in body.findall("p"):
            consider("", p)
    scored.sort(key=lambda x: x[0], reverse=True)
    return [[sec or "نص البحث", clip(text)] for _, sec, text in scored[:top]]


def jats_meta(art):
    title_el = art.find(".//article-meta//article-title")
    title = clean("".join(title_el.itertext())) if title_el is not None else ""
    pmc_id = ""
    for aid in art.findall(".//article-meta/article-id"):
        val = (aid.text or "").strip()
        if aid.get("pub-id-type") in ("pmc", "pmcid") and val:
            pmc_id = val.replace("PMC", "")
    return {
        "title": title,
        "pmc_id": pmc_id,
        "journal": clean(art.findtext(".//journal-meta//journal-title")),
        "year": clean(art.findtext(".//article-meta/pub-date/year")),
        "author": clean(art.findtext(".//article-meta//contrib/name/surname")),
    }


def pmc_ids(query, limit=3):
    r = http_get(EUTILS + "esearch.fcgi",
                 {"db": "pmc", "term": f"({query}) AND open access[filter]",
                  "retmax": limit, "retmode": "json", "sort": "relevance",
                  "tool": "pbl_searcher"})
    return r.json()["esearchresult"]["idlist"]


def fetch_pmc_cards(ids, terms, section_kw):
    if not ids:
        return []
    r = http_get(EUTILS + "efetch.fcgi",
                 {"db": "pmc", "id": ",".join(ids), "retmode": "xml",
                  "tool": "pbl_searcher"}, timeout=45)
    root = ET.fromstring(r.content)
    arts = [root] if root.tag == "article" else root.findall(".//article")
    cards = []
    for art in arts:
        m = jats_meta(art)
        pid = m["pmc_id"]
        cards.append({
            "source": "pmc", "uid": f"PMC{pid}" if pid else "",
            "title": m["title"],
            "link": f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pid}/" if pid else "",
            "meta": " · ".join(x for x in [m["journal"], m["year"]] if x),
            "passages": jats_passages(art, terms, section_kw),
            "terms": terms, "extra": "", "extra_label": "",
            "cite": f"{m['author']} et al. {m['title']}. {m['journal']}. {m['year']}. PMC{pid}",
        })
    return cards


def epmc_list(query, limit=6):
    r = http_get(EPMC + "search",
                 {"query": f"({query}) AND OPEN_ACCESS:y AND HAS_FT:y",
                  "format": "json", "pageSize": limit,
                  "resultType": "lite", "synonym": "TRUE"})
    items = r.json().get("resultList", {}).get("result", [])
    return [i for i in items if i.get("pmcid")]


def fetch_epmc_cards(items, terms, section_kw):
    def one(item):
        try:
            r = http_get(EPMC + f"{item['pmcid']}/fullTextXML", timeout=45)
            root = ET.fromstring(r.content)
            art = root if root.tag == "article" else root.find(".//article")
            if art is None:
                return None
            title = clean(item.get("title", ""))
            journal = clean(item.get("journalTitle", ""))
            year = clean(str(item.get("pubYear", "")))
            authors = clean(item.get("authorString", ""))
            return {
                "source": "epmc", "uid": item["pmcid"], "title": title,
                "link": f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id', '')}",
                "meta": " · ".join(x for x in [journal, year] if x),
                "passages": jats_passages(art, terms, section_kw),
                "terms": terms, "extra": "", "extra_label": "",
                "cite": f"{authors} {title}. {journal}. {year}. {item['pmcid']}",
            }
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(one, items))
    return [c for c in results if c]


def _content_text(node):
    if node is None:
        return ""
    text = "".join(node.itertext())
    return clean(html.unescape(re.sub(r"<[^>]+>", "", text)))


def search_medline(query, terms, limit=3):
    r = http_get(MEDLINE, {"db": "healthTopics", "term": query,
                           "retmax": limit, "tool": "pbl_searcher"})
    root = ET.fromstring(r.content)
    cards = []
    for doc in root.iter("document"):
        fields = {c.get("name", "").lower(): c for c in doc.findall("content")}
        title = _content_text(fields.get("title"))
        summary = (_content_text(fields.get("fullsummary"))
                   or _content_text(fields.get("snippet")))
        url = doc.get("url", "")
        sents = best_sentences(summary, terms, n=3, fallback=2) if summary else []
        cards.append({
            "source": "medline", "uid": url, "title": title, "link": url,
            "meta": "MedlinePlus (NLM)",
            "passages": [["ملخص", s] for s in sents],
            "terms": terms,
            "extra": summary, "extra_label": "الملخص كامل",
            "cite": f"MedlinePlus. {title}. {url}",
        })
    return cards


def do_search(query, selected, section_label, library):
    """Run the search over the chosen sources. Returns a results dict."""
    terms = get_terms(query)
    kw = SECTION_KEYWORDS[section_label]
    cards, errors, suggestion = [], [], None

    if "own" in selected:
        cards += search_own(library, query)

    online = [s for s in selected if s != "own"]
    if online:
        with ThreadPoolExecutor(max_workers=6) as ex:
            stage1 = {}
            if "pubmed" in selected:
                stage1["pubmed"] = ex.submit(search_pubmed, query, terms)
            if "medline" in selected:
                stage1["medline"] = ex.submit(search_medline, query, terms)
            if "pmc" in selected:
                stage1["pmc"] = ex.submit(pmc_ids, query)
            if "epmc" in selected:
                stage1["epmc"] = ex.submit(epmc_list, query)
            if not is_arabic(query):
                stage1["spell"] = ex.submit(espell, query)

            res = {}
            for key, fut in stage1.items():
                try:
                    res[key] = fut.result()
                except Exception:
                    res[key] = None
                    if key != "spell":
                        errors.append(key)
            suggestion = res.get("spell")
            cards += res.get("pubmed") or []
            cards += res.get("medline") or []

            pmc_list = res.get("pmc") or []
            taken = {f"PMC{i}" for i in pmc_list}
            epmc_items = [i for i in (res.get("epmc") or [])
                          if i.get("pmcid") not in taken][:3]

            stage2 = {}
            if pmc_list:
                stage2["pmc"] = ex.submit(fetch_pmc_cards, pmc_list, terms, kw)
            if epmc_items:
                stage2["epmc"] = ex.submit(fetch_epmc_cards, epmc_items, terms, kw)
            for key, fut in stage2.items():
                try:
                    cards += fut.result()
                except Exception:
                    if key not in errors:
                        errors.append(key)

    order = list(SOURCES)
    cards.sort(key=lambda c: order.index(c["source"]))
    return {"query": query, "cards": cards, "errors": errors,
            "suggestion": suggestion}


# =====================================================================
# Session state and callbacks
# =====================================================================
DEFAULTS = {
    "library": {},      # name -> {"kind": "pdf"/"txt", "pages": [[n, text]]}
    "blobs": {},        # name -> original file bytes
    "drafts": [],       # saved texts
    "results": None,
    "up_key": 0,
    "restore_key": 0,
    "q": "",
    "topic": "",
    "notes": [],        # messages shown at the top
}
for _k, _v in DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = copy.deepcopy(_v)


if "pending_topic" in st.session_state:
    st.session_state.topic = st.session_state.pop("pending_topic")


def note(kind, msg):
    st.session_state.notes.append((kind, msg))


def add_doc(name, data):
    kind, pages = parse_file(name, data)
    st.session_state.library[name] = {"kind": kind, "pages": pages}
    st.session_state.blobs[name] = data
    return any((t or "").strip() for _, t in pages)


def delete_doc(name):
    st.session_state.library.pop(name, None)
    st.session_state.blobs.pop(name, None)


def save_draft(i):
    card = st.session_state.results["cards"][i]
    text = "\n\n".join(f"[{lab}] {txt}" if lab else txt
                       for lab, txt in card["passages"])
    if not text:
        text = card.get("extra", "")
    for d in st.session_state.drafts:
        if d["title"] == card["title"] and d["text"] == text:
            st.toast("محفوظ من قبل")
            return
    st.session_state.drafts.append({
        "title": card["title"], "source": card["source"],
        "link": card["link"], "text": text, "cite": card["cite"],
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    st.toast("تم الحفظ بالمسودات ✅")


def delete_draft(i):
    if 0 <= i < len(st.session_state.drafts):
        st.session_state.drafts.pop(i)


def clear_drafts():
    st.session_state.drafts = []


def use_suggestion(text):
    st.session_state.q = text
    st.session_state.run_again = True


def new_topic():
    if st.session_state.get("nt_sources", True):
        st.session_state.library = {}
        st.session_state.blobs = {}
    if st.session_state.get("nt_drafts", True):
        st.session_state.drafts = []
    st.session_state.results = None
    st.session_state.topic = ""
    st.session_state.q = ""


def drafts_text():
    lines = [f"باحث PBL — {st.session_state.topic or 'ملاحظات'}",
             datetime.now().strftime("%Y-%m-%d"), "=" * 40, ""]
    for n, d in enumerate(st.session_state.drafts, start=1):
        lines += [f"{n}. {d['title']}",
                  f"المصدر: {SOURCES.get(d['source'], d['source'])}"]
        if d["link"]:
            lines.append(f"الرابط: {d['link']}")
        lines += ["", d["text"], "", f"المرجع: {d['cite']}", "-" * 40, ""]
    return "\n".join(lines)


def session_json():
    return json.dumps({
        "version": 1,
        "topic": st.session_state.topic,
        "library": st.session_state.library,
        "drafts": st.session_state.drafts,
    }, ensure_ascii=False)


# Load the built-in sample sources once per session
if "sample_loaded" not in st.session_state:
    for _folder in ("sample_docs", "sample_docs.txt"):
        if os.path.isdir(_folder):
            for _f in sorted(os.listdir(_folder)):
                if _f.lower().endswith((".pdf", ".txt")):
                    try:
                        _kind, _pages, _data = load_sample_file(
                            os.path.join(_folder, _f))
                        st.session_state.library[_f] = {"kind": _kind,
                                                        "pages": _pages}
                        st.session_state.blobs[_f] = _data
                    except Exception:
                        pass
            break
    st.session_state.sample_loaded = True


# =====================================================================
# UI
# =====================================================================
st.markdown(CSS, unsafe_allow_html=True)
st.title("🔎 باحث PBL")
st.caption("ابحثي مرة وحدة بمصادرك وبمصادر طبية معتمدة، وخذي الفقرة المناسبة مع مرجعها")

for _kind, _msg in st.session_state.notes:
    getattr(st, _kind)(_msg)
st.session_state.notes = []

tab_search, tab_sources, tab_drafts, tab_session = st.tabs(
    ["🔎 البحث", "📚 المصادر", "📝 المسودات", "⚙️ الجلسة"])


# ---------------------------- Search tab ----------------------------
def render_card(i, c):
    with st.container(border=True):
        st.markdown(
            f"<div class='badge'>{SOURCES[c['source']]}</div>"
            f"<div class='ctitle' dir='auto'>{html.escape(c['title'] or 'بدون عنوان')}</div>",
            unsafe_allow_html=True)
        if c["meta"]:
            st.caption(c["meta"])
        if c["link"]:
            st.markdown(f"[🔗 فتح المصدر الأصلي]({c['link']})")
        for label, text in c["passages"]:
            if label:
                st.caption(label)
            st.markdown(
                f"<div class='passage' dir='auto'>{highlight(text, c['terms'])}</div>",
                unsafe_allow_html=True)
        if not c["passages"]:
            st.caption("ما لقيت فقرة مطابقة بهذا القسم")
        if c.get("extra"):
            with st.expander(c["extra_label"] or "المزيد"):
                st.markdown(
                    f"<div class='viewer' dir='auto'>{highlight(c['extra'], c['terms'])}</div>",
                    unsafe_allow_html=True)
        st.button("💾 احفظ بالمسودات", key=f"save_{i}",
                  on_click=save_draft, args=(i,))


with tab_search:
    with st.form("search_form"):
        st.text_input("اكتبي كلمة أو موضوع للبحث", key="q",
                      placeholder="مثال: liver functions")
        col1, col2 = st.columns(2)
        with col1:
            selected = st.multiselect(
                "المصادر", list(SOURCES), default=list(SOURCES),
                format_func=lambda k: SOURCES[k])
        with col2:
            section = st.selectbox("القسم المطلوب من الأبحاث الكاملة",
                                   list(SECTION_KEYWORDS))
        submitted = st.form_submit_button("🔎 بحث")

    run_again = st.session_state.pop("run_again", False)
    if submitted or run_again:
        query = st.session_state.q.strip()
        if not query:
            st.warning("اكتبي كلمة للبحث")
        elif not selected:
            st.warning("اختاري مصدر واحد على الأقل")
        else:
            with st.spinner("جاري البحث بالمصادر..."):
                st.session_state.results = do_search(
                    query, selected, section, st.session_state.library)

    results = st.session_state.results
    if results:
        if is_arabic(results["query"]):
            st.caption("💡 الأبحاث الطبية بالإنجليزي، اكتبي المصطلح بالإنجليزي لنتائج أفضل")
        if results["suggestion"]:
            st.info(f"هل تقصد: **{results['suggestion']}**؟")
            st.button(f"ابحثي عن: {results['suggestion']}",
                      on_click=use_suggestion, args=(results["suggestion"],))
        for key in results["errors"]:
            st.warning(f"تعذر الاتصال بمصدر: {SOURCES.get(key, key)}")

        cards = results["cards"]
        if not cards:
            st.info("ما لقيت نتائج. جربي كلمات ثانية أو مصادر أكثر.")
        else:
            counts = {}
            for c in cards:
                counts[c["source"]] = counts.get(c["source"], 0) + 1
            st.write(" | ".join(f"{SOURCES[k]}: {v}" for k, v in counts.items()))
            show = st.multiselect("فلتر العرض (فارغ = الكل)", list(counts),
                                  format_func=lambda k: SOURCES[k])
            for i, c in enumerate(cards):
                if not show or c["source"] in show:
                    render_card(i, c)


# ---------------------------- Sources tab ---------------------------
with tab_sources:
    st.write("ارفعي ملفاتك (PDF نصي أو TXT). تبقى هنا طوال الجلسة لحد ما تحذفينها.")
    ups = st.file_uploader("رفع مصادر", type=["pdf", "txt"],
                           accept_multiple_files=True,
                           key=f"up_{st.session_state.up_key}")
    if ups:
        for f in ups:
            try:
                has_text = add_doc(f.name, f.getvalue())
                if has_text:
                    note("success", f"تمت إضافة: {f.name}")
                else:
                    note("warning", f"{f.name}: ما لقيت نص بالملف (يمكن صور ممسوحة)")
            except Exception:
                note("error", f"تعذرت قراءة الملف: {f.name}")
        st.session_state.up_key += 1
        st.rerun()

    library = st.session_state.library
    if not library:
        st.info("ما كو مصادر بعد")
    last_terms = []
    if st.session_state.results:
        last_terms = get_terms(st.session_state.results["query"])
    for name, doc in list(library.items()):
        pages = doc["pages"]
        unit = "صفحة" if doc["kind"] == "pdf" else "جزء"
        with st.expander(f"📄 {name} ({len(pages)} {unit})"):
            num = 1
            if len(pages) > 1:
                num = st.number_input(f"رقم ال{unit}", min_value=1,
                                      max_value=len(pages), value=1, step=1,
                                      key=f"pg_{name}")
            text = pages[int(num) - 1][1] or "(لا يوجد نص بهذه الصفحة)"
            st.markdown(
                f"<div class='viewer' dir='auto'>{highlight(text, last_terms)}</div>",
                unsafe_allow_html=True)
            if name in st.session_state.blobs:
                st.download_button("⬇️ تنزيل الملف الأصلي",
                                   data=st.session_state.blobs[name],
                                   file_name=name, key=f"dl_{name}")
            st.button("🗑 حذف من المصادر", key=f"del_{name}",
                      on_click=delete_doc, args=(name,))


# ---------------------------- Drafts tab ----------------------------
with tab_drafts:
    drafts = st.session_state.drafts
    if not drafts:
        st.info("ما كو مسودات. اضغطي زر 💾 تحت أي نتيجة لحفظها هنا.")
    else:
        st.download_button("⬇️ تنزيل المسودات (ملف نصي)",
                           data=drafts_text().encode("utf-8-sig"),
                           file_name="pbl_notes.txt", mime="text/plain")
        st.button("🗑 مسح كل المسودات", on_click=clear_drafts)
        for i, d in enumerate(drafts):
            with st.container(border=True):
                st.markdown(
                    f"<div class='badge'>{SOURCES.get(d['source'], d['source'])}</div>"
                    f"<div class='ctitle' dir='auto'>{html.escape(d['title'])}</div>",
                    unsafe_allow_html=True)
                if d["link"]:
                    st.markdown(f"[🔗 فتح المصدر الأصلي]({d['link']})")
                st.markdown(
                    f"<div class='passage' dir='auto'>{html.escape(d['text'])}</div>",
                    unsafe_allow_html=True)
                st.caption(f"المرجع: {d['cite']}")
                st.button("🗑 حذف", key=f"deld_{i}",
                          on_click=delete_draft, args=(i,))


# ---------------------------- Session tab ---------------------------
with tab_session:
    st.text_input("اسم الموضوع (اختياري)", key="topic")

    st.subheader("💾 حفظ الجلسة")
    st.caption("ينزل ملف فيه مصادرك (نصوصها) ومسوداتك، وترجعينه بأي وقت.")
    st.download_button("⬇️ تنزيل الجلسة", data=session_json(),
                       file_name="pbl_session.json", mime="application/json")

    st.subheader("📂 استرجاع جلسة")
    session_file = st.file_uploader(
        "ارفعي ملف الجلسة", type=["json"],
        key=f"restore_{st.session_state.restore_key}")
    if session_file is not None and st.button("استرجاع", key="do_restore"):
        try:
            data = json.loads(session_file.getvalue().decode("utf-8"))
            for name, doc in data.get("library", {}).items():
                st.session_state.library[name] = {"kind": doc["kind"],
                                                  "pages": doc["pages"]}
            for d in data.get("drafts", []):
                if d not in st.session_state.drafts:
                    st.session_state.drafts.append(d)
            if data.get("topic"):
                st.session_state.pending_topic = data["topic"]
            note("success", "تم استرجاع الجلسة")
        except Exception:
            note("error", "ملف الجلسة غير صالح")
        st.session_state.restore_key += 1
        st.rerun()

    st.subheader("🆕 موضوع جديد")
    st.caption("نزلي ملاحظاتك الحالية أول، بعدها ابدئي موضوع جديد بصفحة نظيفة.")
    st.download_button("⬇️ تنزيل ملاحظات الموضوع الحالي",
                       data=drafts_text().encode("utf-8-sig"),
                       file_name="pbl_notes.txt", mime="text/plain",
                       key="dl_notes_session")
    st.checkbox("مسح المصادر القديمة", value=True, key="nt_sources")
    st.checkbox("مسح المسودات القديمة", value=True, key="nt_drafts")
    st.button("🆕 ابدأ موضوع جديد", on_click=new_topic)
