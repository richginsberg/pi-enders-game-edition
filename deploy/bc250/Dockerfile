# llama.cpp (Vulkan/RADV) for the AMD BC-250 — divide-and-conquer S3 node.
#
# Why Vulkan, not ROCm: the BC-250 (gfx1013 "Cyan Skillfish") runs great on RADV
# (Mesa), and ROCm support is community-hacked. Verified on the node: Qwen3-Coder-30B
# -A3B q4 at ~74 tok/s. RADV is a USERSPACE driver, so this image builds on any x86_64
# host (no GPU needed — just glslc + Vulkan headers) and only needs the GPU at run time.
#
# Fedora base matches the node's OS/Mesa; the container ships its own RADV (userspace)
# and talks to the host kernel amdgpu via /dev/dri/renderD128.
#
# Build (on any x86_64 box with docker/podman):
#   docker build -t dnc/llamacpp-bc250:latest deploy/bc250
# Run: see deploy/bc250/README.md (needs --device /dev/dri --group-add render).
FROM fedora:40

# Build toolchain + Vulkan build deps (glslc via libshaderc) AND the RADV runtime.
RUN dnf -y install \
        gcc gcc-c++ cmake git \
        vulkan-headers vulkan-loader vulkan-loader-devel libshaderc libshaderc-devel \
        mesa-vulkan-drivers vulkan-tools \
    && dnf clean all && rm -rf /var/cache/dnf

# Pin for reproducibility; override with --build-arg LLAMA_CPP_REF=<tag>.
ARG LLAMA_CPP_REF=master
RUN git clone --depth 1 --branch "${LLAMA_CPP_REF}" https://github.com/ggml-org/llama.cpp /src \
    && cmake -S /src -B /src/build \
        -DGGML_VULKAN=1 -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF -DLLAMA_CURL=OFF \
    && cmake --build /src/build --config Release -j"$(nproc)" --target llama-server \
    && install -m755 /src/build/bin/llama-server /usr/local/bin/llama-server \
    && rm -rf /src

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Model weights and the chat template are mounted at run time (weights are ~9GB and
# the BC-250 has only ~12GB VRAM / 3.5GB host RAM — never bake them into the image).
ENV DNC_MODEL=/models/model.gguf \
    DNC_CTX=65536 DNC_NGL=99 DNC_KV=q8_0 DNC_TEMP=0.6 DNC_PORT=8080 \
    DNC_ALIAS=Qwen-Qwen3-Coder-30B-A3B-Instruct

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
