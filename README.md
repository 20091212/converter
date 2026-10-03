# 统一格式转换器（Unified Converter）

一个**完全本地运行**的桌面批量格式转换工具：音频文件交给 FFmpeg，办公文档交给
LibreOffice，界面用 PySide6 编写。**不需要联网、不上传任何文件、不依赖任何在线
API**（没有 OpenAI、没有 CloudConvert），断网也能用。

- 音频互转：`mp3` `wav` `flac` `aac` `m4a` `ogg` `opus`（源格式更宽松：wma、aiff、ape、alac……）
- 办公文档互转：`docx` `doc` `xlsx` `xls` `pptx` `ppt` `odt` `ods` `odp` `rtf` `txt` `csv` `html` 等
- 批量处理、拖拽添加、实时进度、分级着色日志、取消中止、失败隔离、缺失依赖自动降级（标记「跳过」而不崩溃）

---

## 1. 项目简介

| 项目 | 说明 |
| --- | --- |
| 界面 | PySide6（Qt 6），单窗口，可自由缩放，支持深浅拖拽 |
| 音频引擎 | 系统安装的 **FFmpeg**（命令行调用，`-progress pipe:1` 读取实时进度） |
| 办公引擎 | 系统安装的 **LibreOffice**（`soffice --headless --convert-to`） |
| 并发 | `QThread` 后台线程逐个文件转换，主界面永不卡死；单个失败不影响其它文件 |
| 隐私 | 100% 本地进程调用，无任何网络请求 |
| 平台 | Windows / macOS / Linux |

## 2. 目录结构

```
format-converter/
├── main.py            # 程序入口：日志、QApplication、依赖检测、异常兜底
├── gui.py             # 主窗口与全部界面逻辑（文件列表 / 设置 / 进度 / 日志）
├── converter.py       # 转换核心：FFmpeg 与 LibreOffice 的调用、任务规划、取消机制
├── worker.py          # 后台转换线程 QThread + 统计信息
├── utils.py           # 依赖检测、格式判断、过滤器映射、文件遍历等辅助函数
├── selftest.py        # 自检脚本：真实跑一遍转换链路（含界面离屏冒烟测试）
├── build.py           # 一键打包脚本（PyInstaller 包装）
├── run.bat            # Windows 一键启动（无控制台窗口，自动清理 PYTHONPATH）
├── assets/app.ico     # 应用图标（也可给 build.py --icon 使用）
├── requirements.txt   # Python 依赖（只有 PySide6）
└── README.md
```

## 3. 依赖安装

### 3.1 外部程序（必须，二者都可只装其中一个）

| 平台 | FFmpeg | LibreOffice |
| --- | --- | --- |
| Windows | `winget install Gyan.FFmpeg` | `winget install TheDocumentFoundation.LibreOffice` |
| macOS | `brew install ffmpeg` | `brew install --cask libreoffice` |
| Linux | `sudo apt install ffmpeg` | `sudo apt install libreoffice` |

> 国内网络下 winget 从官网下载 LibreOffice 可能超时，可改用镜像手动安装：
> 从 `https://mirrors.ustc.edu.cn/tdf/libreoffice/stable/<版本>/win/x86_64/` 下载
> `LibreOffice_<版本>_Win_x86-64.msi` 后双击安装即可。

### 3.2 Python 依赖

需要 Python 3.10 或更高版本。

```bash
pip install -r requirements.txt
# 国内网络慢的话：
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

`requirements.txt` 内容：

```
PySide6>=6.6,<7
```

### 3.3 检查依赖是否装好

```bash
python selftest.py           # 全链路自检（推荐首次运行时执行）
python selftest.py --gui     # 额外做一次界面冒烟测试
```

也可以启动程序后按 **F2**（帮助 → 环境检测）查看 FFmpeg / LibreOffice 的路径与版本。

## 4. 运行方式

```bash
python main.py
```

Windows 上也可以直接双击项目里的 `run.bat`（无控制台窗口一闪而过），
或使用桌面快捷方式「统一格式转换器」（指向 `.venv\Scripts\pythonw.exe main.py`）。

> `run.bat` 里有一行 `set PYTHONPATH=`：如果你的 Python 是通过其它工具（例如 Hermes、
> conda、某些启动器）间接启动的，它们可能会注入 `PYTHONPATH` 指向别的虚拟环境，
> 从而遮蔽本项目 `.venv` 里的 PySide6。清空它可以避免这类“包找不到/版本不对”的怪问题。

日志文件位置：

- Windows：`%LOCALAPPDATA%\UnifiedConverter\logs\converter.log`
- macOS：`~/Library/Application Support/UnifiedConverter/logs/converter.log`
- Linux：`~/.local/share/UnifiedConverter/logs/converter.log`

## 5. 使用说明

1. **添加文件**：点「添加文件」/「添加文件夹」，或直接把文件、文件夹**拖进窗口**。
   表格列：文件名、类型、大小、状态、输出路径；状态共 6 种：等待 / 转换中 / 成功 / 失败 / 跳过 / 已取消。
2. **选择目标格式**：下拉框分两组 —— 「音频格式」「办公文档格式」，选好后面板会提示
   「音频文件/办公文档将被标记为跳过」以及依赖是否就绪。
3. **输出目录**：留空 = 输出到每个源文件所在目录；也可以点「浏览…」统一指定。
4. **选项**：
   - 「递归子目录」：添加文件夹时是否包含所有子文件夹；
   - 「覆盖已有文件」：勾选则直接覆盖，未勾选则自动加序号（`歌曲_1.mp3`）避免误删；
   - 「音频码率」：默认 `192k`，只对有损音频（mp3/aac/m4a/ogg/opus）生效，无损格式（wav/flac）自动忽略。
5. **开始转换**：点「开始转换」或按 `Ctrl+R`。总体进度条 + 当前文件进度条实时更新
   （LibreOffice 不提供进度，此时显示不确定进度条，同时显示当前文件名）。
6. **取消**：「取消转换」会安全终止当前文件（先 terminate，必要时 kill / taskkill 结束进程树），
   后续任务不再启动，已完成的结果保留。
7. **查看日志**：底部日志窗口按级别着色（INFO 灰黑 / WARNING 橙 / ERROR 红），
   可按级别过滤、清空、保存到文件。
8. **打开输出目录**：转换完成后统计信息会显示在状态栏；有失败项时会弹窗列出明细，
   并可直接「打开输出目录」。

### 快捷键

| 快捷键 | 功能 |
| --- | --- |
| `Ctrl+O` / `Ctrl+Shift+O` | 添加文件 / 添加文件夹 |
| `Ctrl+R` / `Ctrl+.` | 开始转换 / 取消转换 |
| `Ctrl+E` | 打开输出目录 |
| `Delete` | 移除选中行 |
| `Ctrl+L` | 清空列表 |
| `F1` / `F2` | 使用说明 / 环境检测 |

### 转换命令对照

音频（FFmpeg）：

```bash
ffmpeg -y -hide_banner -nostdin -loglevel error -progress pipe:1 -i input.wma -vn -b:a 192k -c:a libmp3lame output.mp3
```

办公（LibreOffice）：

```bash
soffice -env:UserInstallation=file:///.../UnifiedConverter/lo_profile \
        --headless --norestore --nolockcheck --nodefault --nofirststartwizard \
        --convert-to "pdf:writer_pdf_Export" --outdir <输出目录> <源文件>
```

常用过滤器映射（程序内已内置，含失败回退）：

| 目标 | 过滤器 |
| --- | --- |
| pdf | 按源文档类型自动选择：`writer_pdf_Export` / `calc_pdf_Export` / `impress_pdf_Export` |
| docx | `docx:MS Word 2007 XML` |
| xlsx | `xlsx:Calc MS Excel 2007 XML` |
| pptx | `pptx:Impress MS PowerPoint 2007 XML` |
| odt / ods / odp | `odt:writer8` / `ods:calc8` / `odp:impress8` |
| html | `html:HTML (StarWriter)` |
| txt | `txt:Text (encoded):UTF8` → 回退 `txt:Text` |
| csv | `csv:Text - txt - csv (StarCalc):44,34,76,1` → 回退无参数形式 |
| rtf | `rtf:Rich Text Format` |

## 6. 打包建议（PyInstaller）

```bash
pip install pyinstaller
python build.py                     # 目录模式（onedir，启动快，推荐）
python build.py --onefile           # 单文件模式（体积大、启动稍慢）
python build.py --icon app.ico      # 带图标
```

`build.py` 等价于下面这条命令（已自动排除用不到的 Qt 模块以缩小体积）：

```bash
pyinstaller --noconfirm --clean --windowed --name "统一格式转换器" \
  --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick \
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.Qt3DCore \
  --exclude-module PySide6.QtMultimedia --exclude-module PySide6.QtCharts \
  --exclude-module PySide6.QtDataVisualization --exclude-module PySide6.QtNetworkAuth \
  --exclude-module PySide6.QtPdf --exclude-module PySide6.QtSql \
  --exclude-module PySide6.QtTest --exclude-module tkinter \
  main.py
```

**重要**：FFmpeg 和 LibreOffice **不会被打进 exe**，目标机器仍需自行安装。
如果希望做成「解压即用」，可以把 `ffmpeg.exe`（`ffprobe.exe`）放到 exe 同目录或
`exe目录/bin/` 下 —— 程序会优先使用随包分发的便携版；LibreOffice 便携版同理，
放到 `exe目录/LibreOffice/program/` 即可被自动识别。

## 7. 常见问题（FAQ）

**Q1：提示「未检测到 FFmpeg / LibreOffice」，但我的确装了？**
A：按 `F2` 打开环境检测点「重新检测」。程序会在 PATH、常见安装目录、winget/scoop/
chocolatey 目录、以及 exe 同目录中查找。仍找不到时，可临时用环境变量指定绝对路径：

```bash
set FFMPEG_BINARY=D:\tools\ffmpeg\bin\ffmpeg.exe      # Windows（PowerShell: $env:FFMPEG_BINARY="..."）
set SOFFICE_BINARY=D:\LibreOffice\program\soffice.exe
export FFMPEG_BINARY=/usr/local/bin/ffmpeg             # macOS / Linux
```

**Q2：为什么选了一个格式后，某些文件被标成「跳过」？**
A：跳过是**预期行为**，不会影响其它文件。常见原因：音频↔办公类型不匹配、源文件已是目标
格式且输出目录相同、缺少对应引擎、LibreOffice 不支持的跨类型转换（如 xlsx → docx 这类
「表格转文字文档」）。鼠标悬停在「状态」单元格上可以看到完整原因。

**Q3：xlsx / pptx 转 pdf 有排版变化吗？**
A：LibreOffice 的渲染与 Microsoft Office 存在差异，复杂排版（特殊字体、图表、SmartArt）
可能有轻微偏移，建议转换后抽查。

**Q4：扫描版 PDF 能转 Word 吗？**
A：不能。本工具不做 OCR，纯图片的 PDF 转出来只有图片，Word 里没有可编辑文字。

**Q5：PDF 转 Word（docx）保排版吗？**
A：效果有限。PDF 是排版描述格式，转 docx 后复杂版式（分栏、多字体混排、公式）通常会走形，
简单文档效果尚可。

**Q6：转换会不会把我的文件传到网上？**
A：不会。程序只调用本机的 ffmpeg / soffice 进程，没有任何网络代码；源文件只读，输出写在
你指定的目录。

**Q7：macOS 上第一次点「开始转换」没反应？**
A：系统「隐私与安全性」可能会拦截未签名的 Python 进程调用外部程序，允许一次即可；
打包成 app 后若被 Gatekeeper 拦截，执行 `xattr -dr com.apple.quarantine <app路径>`。

**Q8：Linux 上「打开输出目录」没反应？**
A：需要 `xdg-open`（`sudo apt install xdg-utils`）。

**Q9：转换大文件时界面卡住？**
A：界面不会卡（转换在后台线程）。若某个文件长时间无进展，可点「取消转换」；
LibreOffice 处理超大文档时启动较慢，属正常现象。单个文件默认超时：音频 30 分钟、办公 10 分钟。

**Q10：能转视频吗？**
A：当前版本只处理音频与办公文档。`-vn` 会丢弃输入中的视频轨道，因此把视频拖进来做音频提取
（例如 mp4 → mp3）是可以的。

## 8. 注意事项与已知限制

- 完全本地运行；音频依赖 FFmpeg，办公文档依赖 LibreOffice，缺一不可（只影响对应类别的转换）。
- 不支持扫描版 PDF 的 OCR。
- PDF → Word 的排版还原能力有限。
- 音频「转码」不是「无损放大」：有损格式之间互转会二次损失，追求音质请转 `wav` / `flac`。
- 转换`.doc`(旧版)、`.ppt`(旧版) 等老格式时，若文件所在目录为只读或系统目录，可能失败，
  建议先把源文件复制到普通目录再试。
- LibreOffice 转换使用独立用户配置目录（`%LOCALAPPDATA%\UnifiedConverter\lo_profile`），
  不会与你正在使用的 LibreOffice 冲突；首次调用会慢几秒属于正常。
