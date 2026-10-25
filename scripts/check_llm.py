import os

from openai import OpenAI


def main() -> None:
    with OpenAI(
        api_key=os.environ["LLM_API_KEY"],
        base_url=os.environ["LLM_BASE_URL"],
        timeout=30,
        max_retries=0,
    ) as client:
        response = client.chat.completions.create(
            model=os.environ["LLM_MODEL"],
            messages=[{"role": "user", "content": "请只回复：模型连接成功"}],
            max_completion_tokens=64,
            extra_body={"enable_thinking": "false"},
        )

        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("请求已返回，但没有文本输出")

        print("模型：", response.model)
        print("回复：", content)
        print("Token用量：", response.usage)


if __name__ == "__main__":
    main()
