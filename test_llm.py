from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="gemini-3-flash-preview",
    api_key="AIzaSyD-YuTmR_yNagdJeK62kblt0foCmYz65wk",
    # Note: The /openai/ part and the trailing slash are critical
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

try:
    response = llm.invoke("Hello, who are you?")
    print(response.content)
except Exception as e:
    print(f"Error: {e}")