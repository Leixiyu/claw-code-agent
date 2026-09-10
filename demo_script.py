import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_URL = "http://183.11.226.132:8765/api/chat/stream"

PROMPT = "只检查 Agent Workspace 并简要说明可见目录，不要修改任何内容。"


def main():
    request = Request(
        API_URL,
        data=json.dumps({"prompt": PROMPT}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=600) as response:
            for line in response:
                if not line.strip():
                    continue
                event = json.loads(line)
                if event["type"] == "tool_start":
                    print(f"[进度] {event['message']}", flush=True)
                elif event["type"] == "result":
                    print(event["data"].get("final_output") or "服务未返回回复。")
                    return
                elif event["type"] == "error":
                    print(event.get("error") or "服务返回错误。")
                    return
            print("连接已结束，但未收到最终回答。请查看会话后再决定是否重试。")

    except HTTPError as exc:
        print(f"请求失败：HTTP {exc.code}")
    except URLError as exc:
        print(f"连接失败：{exc.reason}")
    except TimeoutError:
        print("等待响应超时。")
    except (ValueError, KeyError):
        print("服务返回的数据格式异常。")


if __name__ == "__main__":
    main()
