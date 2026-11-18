# 文档转换服务（PDF ↔ Word）· 云托管免域名版

格式工坊小程序里「PDF 转 Word」「Word 转 PDF」的服务端。

小程序通过 `wx.cloud.callContainer` 走微信私有通道调用本服务，**不需要域名、不需要 ICP 备案、不需要配置服务器域名白名单**。

- **Word → PDF**：LibreOffice 无头模式转换，保留排版
- **PDF → Word**：pdf2docx 提取文字与表格，生成可编辑的 `.docx`
- 文件保存在容器本地 `files/<jobId>/`，默认 **30 分钟**后自动删除，不长期留存
- 单文件上限 20MB；Word → PDF 的 LibreOffice 转换超时 110 秒

## 为什么分片

`wx.cloud.callContainer` 有两条硬限制：

1. 单次调用最长 **15 秒**；
2. 响应体约 **1MB**。

所以不能像普通 HTTP 接口那样一次上传、一次下载。本服务把文件拆成任务：

```text
小程序                         云托管容器
  │  POST /api/jobs            │  创建任务
  │  POST /api/jobs/<id>/chunk │  1MB 一片，顺序上传
  │  POST /api/jobs/<id>/start │  后台线程开始转换
  │  GET  /api/jobs/<id>       │  每 1.2 秒轮询一次
  │  GET  /api/jobs/<id>/file  │  512KB 一片，下载结果
```

转换在后台线程执行，轮询接口总是快速返回，因此不会撞上 15 秒超时。

## 接口

### 小程序使用的任务接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/jobs` | JSON：`{ "name": "报告.pdf", "size": 12345, "target": "docx" }`，返回 `jobId` |
| `POST` | `/api/jobs/<jobId>/chunk?offset=0` | 请求体为二进制分片，`offset` 必须等于已上传字节数 |
| `POST` | `/api/jobs/<jobId>/start` | 校验上传完整后开始转换 |
| `GET` | `/api/jobs/<jobId>` | 返回 `status`：`uploading` / `queued` / `converting` / `done` / `error` |
| `GET` | `/api/jobs/<jobId>/file?offset=0&size=524288` | 返回二进制结果分片，单次最大 800KB |

创建任务的响应示例：

```json
{ "ok": true, "jobId": "a1b2c3...", "chunkSize": 1048576 }
```

失败统一返回 `{"ok": false, "error": "中文错误说明"}`。

`target` 只接受：

- `docx`：源文件必须是 `.pdf`
- `pdf`：源文件必须是 `.doc` / `.docx`

### 仅用于本机调试的接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/convert` | `multipart/form-data`，字段 `file` + `target`，同步转换 |
| `GET` | `/files/<token>/<name>` | 下载 `/convert` 的结果 |

小程序不再使用这三个接口。

## 本地运行

需要本机安装 LibreOffice（`soffice` 在 PATH 中）。

```bash
cd server/doc-convert
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py
# 服务在 http://localhost:8080
```

快速验证转换能力：

```bash
curl -F "file=@sample.pdf" -F "target=docx" http://localhost:8080/convert
```

## Docker 部署

镜像已包含 LibreOffice 和思源黑体，约 700MB 左右：

```bash
docker build -t doc-convert .
docker run -d --name doc-convert -p 8080:8080 doc-convert
```

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MAX_SIZE_MB` | `20` | 单文件大小上限，需 ≥ 小程序端上限（10MB） |
| `FILE_TTL_MINUTES` | `30` | 结果文件保留时长 |
| `SOFFICE` | `soffice` | LibreOffice 可执行文件路径 |
| `PORT` | `8080` | 监听端口 |

> 任务状态保存在进程内存中，结果写在容器本地磁盘，因此容器必须**单实例**运行，gunicorn 必须**单 worker + 多线程**（Dockerfile 已配置 `-w 1 --threads 8`）。多实例时，上传分片、轮询和下载可能落到不同容器，任务会 404。

## 微信云托管部署（CLI）

前提：小程序已完成微信认证（个人主体也需要认证），并已在云托管控制台创建环境和服务；Node.js 与 npm 可用。**本机不需要安装 Docker**，构建在云端完成。

1. 安装 CLI，并用控制台「设置 → CLI 密钥」生成的密钥登录（密钥只显示一次）：

```powershell
npm install -g @wxcloud/cli
wxcloud login --appId wx110d28eae08e8452 --privateKey "<粘贴 CLI 密钥>"
wxcloud env:list
wxcloud service:list --envId=<环境ID>
```

2. 在 `server/doc-convert` 目录下部署（端口 8080，代码包方式）：

```powershell
cd "C:\Users\Win\Desktop\文件\server\doc-convert"
wxcloud run:deploy --targetDir=. --dockerfile=Dockerfile --containerPort=8080 `
  --envId=<环境ID> --serviceName=doc-convert --releaseType=FULL --remark="v1" --noConfirm
```

3. 在控制台「服务设置 → 环境变量」中按需添加：

| 变量 | 值 |
| --- | --- |
| `MAX_SIZE_MB` | `20` |
| `FILE_TTL_MINUTES` | `30` |

4. 规格与实例设置：

- 建议至少 **1 核 2G**；
- **最大实例数 = 1**（必须）；最小实例数建议 1，避免冷启动；
- 请求超时 ≥ 30 秒即可，转换本身在后台线程里执行，不受该超时限制。

5. 确认该云托管环境已绑定/授权当前小程序（`AppID` 与小程序的 AppID 一致）。`callContainer` 只允许被授权的小程序调用。

> 不需要绑定自定义域名，也不需要在小程序后台配置 request / uploadFile / downloadFile 合法域名。云托管的默认公网域名可以保持关闭。

## 小程序端配置

编辑 [`utils/config.js`](../../utils/config.js)：

```js
module.exports = {
  CLOUD_ENV: 'prod-1a2b3c',      // 云托管环境 ID
  CLOUD_SERVICE: 'doc-convert',  // 云托管服务名
  MAX_UPLOAD_SIZE: 10 * 1024 * 1024,
}
```

留空时，PDF / Word 页面会提示「云托管尚未配置」，其余本地工具不受影响。

注意事项：

- 微信基础库需 **≥ 2.23.0**（`wx.cloud.callContainer` 的要求）；
- 小程序不需要单独调用 `wx.cloud.init`，每次请求在 `config.env` 中指定环境；
- 服务端的 `X-WX-SERVICE` 请求头必须等于服务名，客户端已自动添加；
- 如果真机提示无法连接云托管，依次检查：环境 ID 是否填错、服务名是否一致、服务是否部署成功、云托管环境是否已授权当前小程序。

### 弱网说明

上传分片默认 1MB、下载分片 512KB，都会在 15 秒内完成。网络很慢时，1MB 上传分片仍可能超时，可在 [`utils/docservice.js`](../../utils/docservice.js) 顶部调小 `UPLOAD_CHUNK`（例如 512KB）；分片越小越稳，但请求次数更多。

## 已知限制

- **扫描件 / 拍照生成的 PDF** 本质是图片，转 Word 后仍是图片内容，不能直接编辑；需要 OCR 才能提取文字。
- 加密 PDF 无法转换，会返回失败提示。
- 复杂排版的 PDF 转 Word 只能尽量还原，不能保证与原版逐像素一致。
- 特别大的 PDF 转 Word 可能耗时数分钟；小程序端最多等待 5 分钟，超时可换小文件重试。
- 服务重启或重新部署会丢失进行中的任务，客户端会提示重新开始；已完成的结果文件也会被清空。
- 同一时刻只执行一个转换（服务端串行处理），多人同时使用时后来者需要排队。
- 容器磁盘为临时存储，请勿依赖它做长期文件存储。
