# 辅助小工具

一个面向 Windows 的本地桌面效率工具。它提供任务表读取、字幕与语音处理、浏览器顺序启动、表格链接监视、库存提醒，以及可选的结果整理和云端写回功能。

## 隐私与配置

仓库和发布包不包含任何真实 API Key、OAuth 凭据、Token、表格链接、客户数据或历史运行记录。首次使用时，将 `config.example.json` 复制为 `config.json`，再通过程序设置填写本机参数。

Google 服务需要用户自行创建 OAuth 桌面客户端，并把相应凭据文件放在程序目录中。程序生成的 `config.json`、Token、监视状态、库存状态和提交日志均已被 Git 忽略。

可选的 AI 视频检测目标、说明、规则和结果复核关键词全部由本机配置提供，源码不包含具体业务判定规则。

## 运行环境

- Windows 10/11
- Python 3.10（从源码运行时）
- Google Chrome（仅浏览器启动功能需要）
- 可选的外部视频编码器及预设（仅启用视频压缩时需要）
- 首次使用字幕识别时需要联网下载 Whisper `base` 模型

从源码运行：

```powershell
python -m pip install -r requirements.txt
python main.py
```

## 发布包

正式发布由 GitHub Actions 在 Windows runner 上使用 PyInstaller 构建。Release 中的 ZIP 由 `github-actions[bot]` 上传，并带有 GitHub Artifact Attestation。

下载后解压整个目录，复制并填写 `config.example.json`，然后运行 `AssistantTool.exe`。不要只移动 EXE；旁边的 `_internal` 目录是运行所需依赖。

验证发布包来源：

```powershell
gh attestation verify AssistantTool-Windows.zip --repo secure-artifacts/REPOSITORY_NAME
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 许可

除非仓库所有者另行提供许可证，否则本项目保留全部权利。
