from fleetd.discover import model_slug, parse_process_line, to_deployment
from fleetd.models import Host, Management, ServerKind, Squad


HOST = Host(id="rig-3090-a", address="10.0.0.21", squad=Squad.S1_HEAVY)


def test_custom_llamacpp_compile_step35():
    # The real-world adoption case: hand-compiled llama.cpp serving Step-3.5-Flash q3
    line = (
        " 41231 /opt/llama.cpp/build/bin/llama-server "
        "-m /models/stepfun-ai_Step-3.5-Flash-GGUF/Step-3.5-Flash-Q3_K_M-00001-of-00004.gguf "
        "--port 8080 -c 65536 -ngl 999 --alias step-3.5-flash --host 0.0.0.0"
    )
    s = parse_process_line(line)
    assert s is not None
    assert s.kind == ServerKind.LLAMACPP
    assert s.pid == 41231
    assert s.binary == "/opt/llama.cpp/build/bin/llama-server"
    assert s.port == 8080
    assert s.facts["ctx"] == "65536"
    assert model_slug(s) == "step-3.5-flash"  # alias wins over path

    dep = to_deployment(HOST, s)
    assert dep.management == Management.ADOPTED
    assert dep.id == "adopted-rig-3090-a-8080"
    assert dep.context_window == 65536
    assert dep.discovered["runner"] == "bare"


def test_gguf_shard_name_slug_without_alias():
    line = (
        " 99 /usr/local/bin/llama-server "
        "--model /models/Qwen3-4B-Q4_K_M-00001-of-00002.gguf --port=8081"
    )
    s = parse_process_line(line)
    assert s is not None
    assert s.port == 8081  # --flag=value form
    assert model_slug(s) == "Qwen3-4B-Q4_K_M"


def test_vllm_detection():
    line = (
        " 512 python3 -m vllm.entrypoints.openai.api_server "
        "--model Qwen/Qwen3-8B --max-model-len 32768 --tensor-parallel-size 1 --port 8000"
    )
    s = parse_process_line(line)
    assert s is not None
    assert s.kind == ServerKind.VLLM
    assert s.facts["model"] == "Qwen/Qwen3-8B"
    assert s.facts["ctx"] == "32768"


def test_vllm_serve_form():
    s = parse_process_line(" 600 vllm serve Qwen/Qwen3-14B --port 8000")
    assert s is not None and s.kind == ServerKind.VLLM


def test_ignores_unrelated_processes():
    for line in (
        "  1 /sbin/init",
        " 20 python3 train.py --model resnet",
        " 30 nginx: worker process",
        " 40 grep llama-server",
    ):
        assert parse_process_line(line) is None


def test_default_port_when_absent():
    s = parse_process_line(" 77 /usr/bin/llama-server -m /m/x.gguf")
    assert s is not None
    assert s.port == 8080
