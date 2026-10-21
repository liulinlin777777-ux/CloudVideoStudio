# CloudVideoStudio

**云端 AI 专业视频工作室**

[English](README.md) · [简体中文](README.zh-CN.md)

CloudVideoStudio 是开源的云原生 AI 视频编辑项目。当前实现是 **P0 CPU 技术原型**：
版本化工程模型、有理数帧率、限定的两条视频轨和两条音轨、真实 FFmpeg 渲染、
受信任的代理媒体插件，以及本机回环预览。AI 剪辑、视频生成、数字人和专业工作台
仍属规划能力，当前并非生产平台发行版。

## 运行技术基线

需要 Python 3.12+、支持 libx264/AAC 的 FFmpeg/FFprobe；候选验证另需 MLT 的 `melt`。
实际验证版本和许可证见 [DEPENDENCIES.md](docs/DEPENDENCIES.md)。

```sh
python -m pip install -e '.[interchange]'
python -m unittest discover -s tests -v
python tools/media_smoke.py --output-dir build/media
python tools/candidate_smoke.py --output-dir build/candidates
python tools/preview_server.py --media-root build/media
```

打开 `http://127.0.0.1:8774` 查看合成素材预览。原型支持 640×360、30000/1001 fps、
120 帧的固定拓扑，包括裁切、交叉转场、画中画、双音轨混音、帧偏移编辑、工程保存/加载和
CPU 渲染回执。不支持的轨道拓扑和效果会被明确拒绝，原始素材保持不变。

独立媒体烟测会真实解码成片，检查关键帧颜色、转场和叠加位置，测量两个音频频率及
音画事件起点，并运行真实代理插件的正常、超时和错误路径。CI 保存可下载的公开合成证据。

## 项目方向

P0–P5 方向见 [ROADMAP.md](ROADMAP.md)。Safari、4K/GPU、专业工作台和已发布的
Codex Cloud 运行环境保持未验证。项目没有新增付费基础设施，也不承诺全天持续运行。

欢迎通过 Issue、Discussions 和 Pull Request 参与贡献。请阅读[贡献指南](CONTRIBUTING.md)、
[行为准则](CODE_OF_CONDUCT.md)与[安全政策](SECURITY.md)。原创代码采用 [MIT](LICENSE)，
第三方组件保留各自许可证。本仓库不包含 FFmpeg/MLT 二进制或第三方源码副本。
