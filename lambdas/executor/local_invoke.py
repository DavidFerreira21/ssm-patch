import json
import os
from pathlib import Path


DEFAULT_ENV = {
    "AWS_REGION": "us-east-1",
    "DDB_TABLE_NAME": "ssm-patch-reboot-requests",
    "ACTIVE_REQUESTS_INDEX_NAME": "gsi1-active-requests",
    "GRACE_HOURS": "8",
    "REBOOT_REQUIRED_TAG_KEY": "RebootRequired",
    "REBOOT_REQUIRED_TAG_VALUE": "true",
}


class FakeContext:
    function_name = "manual-executor-invoke"
    aws_request_id = "manual-executor-request"
    invoked_function_arn = "arn:aws:lambda:local:manual:function:manual-executor-invoke"
    memory_limit_in_mb = 256


BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
EVENT_FILE = BASE_DIR / "dynamodb_stream_event.json"


def load_env_file(env_file: Path) -> None:
    if not env_file.exists():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def apply_env() -> None:
    load_env_file(ENV_FILE)
    for key, value in DEFAULT_ENV.items():
        os.environ.setdefault(key, value)


def main() -> None:
    apply_env()
    import app

    event = json.loads(EVENT_FILE.read_text(encoding="utf-8"))
    result = app.lambda_handler(event, FakeContext())
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
