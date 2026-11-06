import streamlit as st
import os 

st.set_page_config(page_title="PBL Search", page_icon="🔎", layout="wide")

st.title("🔎 PBL Search")
st.write("ابحث مرة واحدة بكل مصادرك")

query = st.text_input("اكتب كلمة أو جملة للبحث")

if query:
    st.write("بحثت عن:", query)

    

st.sidebar.header("المصادر الجاهزة")
for f in os.listdir("sample_docs"):
    st.sidebar.write("📄", f)