import io
import os
import re
import html
import xml.etree.ElementTree as ET

import requests
import streamlit as st
from pypdf import PdfReader

st.set_page_config(page_title="PBL Searcher", page_icon="🔎", layout="wide")

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

# Section filter: label shown on screen -> keywords matched in section titles
SECTION_KEYWORDS = {
    "كل الأقسام": [],
    "المقدمة": ["introduction", "background"],
    "الطرق": ["method", "material"],
    "النتائج": ["result"],
    "المناقشة": ["discussion"],
    "الخلاصة": ["conclusion"],
}


def get_terms(query):
    """Split the query into words longer than 2 letters."""
    terms = [w for w in query.split() if len(w) > 2]
    return terms or [query]


def highlight(text, terms):
    """Escape the text and wrap matched terms in <mark>."""
    text = html.escape(text)
    pattern = "|".join(re.escape(html.escape(t)) for t in terms)
    return re.sub(pattern, lambda m: f"<mark>{m.group(0)}</mark>",
                  text, flags=re.IGNORECASE)


# ---------- own sources (PDF / TXT) ----------

def read_pdf(data):
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        pages.append((i, page.extract_text() or ""))
    return pages


def read_txt(data):
    return [(1, data.decode("utf-8", errors="ignore"))]


@st.cache_data
def load_sample_docs():
    docs = {}
    if not os.path.isdir("sample_docs"):
        return docs
    for name in os.listdir("sample_docs"):
        path = os.path.join("sample_docs", name)
        with open(path, "rb") as f:
            data = f.read()
        if name.lower().endswith(".pdf"):
            docs[name] = read_pdf(data)
        elif name.lower().endswith(".txt"):
            docs[name] = read_txt(data)
    return docs


def search_docs(docs, query):
    terms = [t.lower() for t in get_terms(query)]
    results = []
    for name, pages in docs.items():
        for page_num, text in pages:
            for line in text.split("\n"):
                low = line.lower()
                if all(t in low for t in terms):
                    results.append((name, page_num, line.strip()))
    return results


# ---------- PubMed (abstracts) ----------

def search_pubmed(query, limit=5):
    try:
        r = requests.get(
            EUTILS + "esearch.fcgi",
            params={"db": "pubmed", "term": query, "retmax": limit,
                    "retmode": "json", "sort": "relevance"},
            timeout=15)
        ids = r.json()["esearchresult"]["idlist"]
        if not ids:
            return []
        r = requests.get(
            EUTILS + "efetch.fcgi",
            params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
            timeout=15)
        root = ET.fromstring(r.content)
    except Exception:
        return None

    papers = []
    for art in root.findall(".//PubmedArticle"):
        pmid = art.findtext(".//PMID")
        title_el = art.find(".//ArticleTitle")
        title = "".join(title_el.itertext()) if title_el is not None else ""
        parts = []
        for ab in art.findall(".//Abstract/AbstractText"):
            label = ab.get("Label")
            text = "".join(ab.itertext())
            parts.append(f"{label}: {text}" if label else text)
        papers.append({"pmid": pmid, "title": title, "abstract": parts})
    return papers


# ---------- PMC (full text, open access only) ----------

def search_pmc(query, section_keywords, limit=3):
    try:
        r = requests.get(
            EUTILS + "esearch.fcgi",
            params={"db": "pmc",
                    "term": f"({query}) AND open access[filter]",
                    "retmax": limit, "retmode": "json",
                    "sort": "relevance"},
            timeout=15)
        ids = r.json()["esearchresult"]["idlist"]
        if not ids:
            return []
        r = requests.get(
            EUTILS + "efetch.fcgi",
            params={"db": "pmc", "id": ",".join(ids), "retmode": "xml"},
            timeout=40)
        root = ET.fromstring(r.content)
    except Exception:
        return None

    terms = [t.lower() for t in get_terms(query)]
    papers = []
    for art in root.findall(".//article"):
        pmc_id = ""
        for aid in art.findall(".//article-meta/article-id"):
            val = (aid.text or "").strip()
            if aid.get("pub-id-type") in ("pmc", "pmcid") and val:
                pmc_id = val.replace("PMC", "")

        title_el = art.find(".//article-meta//article-title")
        title = "".join(title_el.itertext()) if title_el is not None else ""

        scored = []
        body = art.find("body")
        if body is not None:
            for sec in body.findall("sec"):
                sec_title = (sec.findtext("title") or "").strip()
                if section_keywords and not any(
                        k in sec_title.lower() for k in section_keywords):
                    continue
                for p in sec.iter("p"):
                    text = "".join(p.itertext()).strip()
                    if len(text) < 40:
                        continue
                    score = sum(text.lower().count(t) for t in terms)
                    if score > 0:
                        scored.append((score, sec_title, text))
        scored.sort(key=lambda x: x[0], reverse=True)
        papers.append({"pmc_id": pmc_id, "title": title,
                       "passages": scored[:3]})
    return papers


# ---------- interface ----------

st.title("🔎 باحث PBL")
st.write("ابحث مرة واحدة بكل مصادرك وبالأبحاث المفتوحة")

docs = dict(load_sample_docs())

uploaded = st.file_uploader("ارفع مصادرك (PDF أو TXT)",
                            type=["pdf", "txt"],
                            accept_multiple_files=True)
for f in uploaded:
    data = f.getvalue()
    if f.name.lower().endswith(".pdf"):
        docs[f.name] = read_pdf(data)
    else:
        docs[f.name] = read_txt(data)

st.sidebar.header("المصادر")
for name in docs:
    st.sidebar.write("📄", name)

scope = st.radio("نطاق البحث",
                 ["مصادري فقط", "PubMed (ملخصات)",
                  "PMC (نص كامل)", "الكل"],
                 horizontal=True)

section_choice = "كل الأقسام"
if scope in ("PMC (نص كامل)", "الكل"):
    section_choice = st.selectbox("القسم المطلوب من الأبحاث",
                                  list(SECTION_KEYWORDS.keys()))

query = st.text_input("اكتب كلمة أو جملة للبحث (الأبحاث بالإنجليزي)")

if query:
    terms = get_terms(query)

    if scope in ("مصادري فقط", "الكل"):
        results = search_docs(docs, query)
        st.subheader(f"📄 مصادرك ({len(results)})")
        for name, page_num, line in results:
            st.markdown(f"**{name}** — صفحة {page_num}")
            st.markdown(highlight(line, terms), unsafe_allow_html=True)
            st.divider()

    if scope in ("PMC (نص كامل)", "الكل"):
        st.subheader("📚 أبحاث مفتوحة (نص كامل)")
        with st.spinner("جاري البحث..."):
            pmc_papers = search_pmc(query, SECTION_KEYWORDS[section_choice])
        if pmc_papers is None:
            st.error("تعذر الاتصال، حاول مرة ثانية")
        elif not pmc_papers:
            st.info("لا توجد أبحاث مفتوحة لهذي الكلمة")
        else:
            for p in pmc_papers:
                link = f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{p['pmc_id']}/"
                st.markdown(f"**{p['title']}**  \n[PMC{p['pmc_id']}]({link})")
                if p["passages"]:
                    for score, sec_title, text in p["passages"]:
                        st.caption(f"القسم: {sec_title or '—'}")
                        st.markdown(highlight(text, terms),
                                    unsafe_allow_html=True)
                else:
                    st.caption("لا توجد فقرة مطابقة بهذا القسم")
                st.divider()

    if scope in ("PubMed (ملخصات)", "الكل"):
        st.subheader("🌐 PubMed (ملخصات)")
        with st.spinner("جاري البحث..."):
            papers = search_pubmed(query)
        if papers is None:
            st.error("تعذر الاتصال، حاول مرة ثانية")
        elif not papers:
            st.info("لا توجد أبحاث لهذه الكلمة")
        else:
            for p in papers:
                link = f"https://pubmed.ncbi.nlm.nih.gov/{p['pmid']}/"
                st.markdown(f"**{p['title']}**  \n[PMID {p['pmid']}]({link})")
                full = " ".join(p["abstract"])
                sentences = re.split(r"(?<=[.!?])\s+", full)
                hits = [s for s in sentences
                        if any(t.lower() in s.lower() for t in terms)]
                for s in hits[:3]:
                    st.markdown(highlight(s, terms), unsafe_allow_html=True)
                with st.expander("الملخص كامل"):
                    st.write(full or "لا يوجد ملخص لهذا البحث")
                st.divider()
