# Vendors Initialization Examples

## Ollama

For installation see: https://ollama.com/

```python
from langchain_community.chat_models import ChatOllama

llm = ChatOllama(model='llava')
```

## OpenAI

```bash
export OPENAI_API_KEY="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"
```

```python
from langchain_openai.chat_models import ChatOpenAI

llm = ChatOpenAI(
    model="gpt-4o",
)
```

## AWS Bedrock

```bash
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_SESSION_TOKEN="..."
```

```python
from langchain_aws.chat_models import ChatBedrock

llm = ChatBedrock(
    model="anthropic.claude-3-opus-20240229-v1:0",
)
```
