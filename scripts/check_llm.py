# 模型连接检查脚本，只检查一问一答能否成功返回文本。
# 采购 Agent 的工具调用和决策循环将在 M1.5 实现。
# 密钥、兼容接口地址和模型名均从环境变量读取，密钥不写入源码和输出。

import os

from openai import OpenAI


def main() -> None:
    # 使用 OpenAI Python SDK 访问配置的兼容接口，实际模型由 LLM_MODEL 决定。
    # with 会在离开代码块时关闭客户端连接资源。
    with OpenAI(
        api_key=os.environ["LLM_API_KEY"],
        base_url=os.environ["LLM_BASE_URL"],
        timeout=30,  # 连接检查的请求超时设置，单位为秒。
        max_retries=0,  # 关闭 SDK 自动重试，方便看清单次调用的问题和用量。
    ) as client:
        # messages 是这次请求的对话；这里只让模型返回一句短文本。
        response = client.chat.completions.create(
            model=os.environ["LLM_MODEL"],
            messages=[{"role": "user", "content": "请只回复：模型连接成功"}],
            max_completion_tokens=64,  # 限制本次检查的生成 token 上限。
            # 传给模型供应商的自定义参数，沿用当前已连接成功的脚本配置。
            extra_body={"enable_thinking": "false"},
        )

        # choices[0] 取第一份回答，message.content 取其中的文本内容。
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("请求已返回，但没有文本输出")

        # usage 是 token 用量；实际费用还需要按供应商的计费配置计算。
        print("模型：", response.model)
        print("回复：", content)
        print("Token用量：", response.usage)


# 只有直接运行本脚本才会发送模型请求，导入模块不会触发 main。
if __name__ == "__main__":
    main()
