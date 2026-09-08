import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_URL = "http://183.11.226.132:8765/api/chat"

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
            result = json.load(response)

        print(result.get("final_output") or result.get("error") or "服务未返回回复。")

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