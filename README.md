# 🎲 可公开核验的现场抽签（Flask + SQLite + 原生 JavaScript）

一个用于现场抽奖/抽签的小型 Web 系统。核心是**承诺—封存—揭晓（commit–seal–reveal）**流程：
种子在浏览器本地生成，服务器事先只拿到 SHA-256 承诺；名单封存后不可改；
揭晓时任何人都能用事先留存的承诺、名单摘要和**公开的确定性散列规则**独立复算结果，
而不是只能相信服务器保存的中奖列表。

无需外部账号、无需付费服务、无需联网 CDN（SHA-256 为自带纯 JS 实现，HTTP 现场环境可用）。

---

## 目录

- [一、信任模型与规则](#一信任模型与规则)
- [二、一条命令启动（Docker Compose）](#二一条命令启动docker-compose)
- [三、访问地址与端口](#三访问地址与端口)
- [四、完整操作示例（网页界面）](#四完整操作示例网页界面)
- [五、完整操作示例（命令行 / API）](#五完整操作示例命令行--api)
- [六、导出核验包与独立核验](#六导出核验包与独立核验)
- [七、不用 Docker 的本地运行方式](#七不用-docker-的本地运行方式)
- [八、项目结构与接口说明](#八项目结构与接口说明)

---

## 一、信任模型与规则

规则标识：`sha256-argmax-v1`，完整文字见 [`RULES.md`](RULES.md)。摘要：

1. **创建轮次（承诺）**：浏览器用密码学随机数生成 32 字节（256 位）种子 `seed`（64 位十六进制），
   只把承诺 `SHA256("COMMIT:"+seed)` 和中奖名额提交给服务器。页面提示保存种子并展示承诺供现场事先留存。
2. **登记名单**：按唯一编号登记，封存前可增删、整体替换；服务器拒绝重复编号。
3. **封存名单**：人数必须 ≥ 名额。封存时把编号按 **UTF-8 字节序**排序得到规范快照，
   连同 `SHA256(快照)` 摘要一次性保存并展示，**此后服务器拒绝任何名单修改**。
4. **揭晓种子**：
   - 提交的种子若 `SHA256("COMMIT:"+seed)` 与承诺不符 → **轮次保持封存状态**，只记录失败次数；
   - 相符 → 按公开规则选出**不重复**的中奖编号，结果与状态在同一事务一次保存；
   - 重复提交种子只返回既有结果，**不会重新抽奖**。
5. **抽奖规则**：第 `i` 轮（`i=0,1,…`）对每个未中奖编号 `id` 计算
   `score = int_be(SHA256("sha256-argmax-v1|"+seed+"|"+id+"|"+i))`，取分数最大者，
   平局取字节序靠前者。全程不取模，没有取模偏置。
6. **弃权与递补（揭晓后）**：可将当前有效中奖编号标记弃权；第 `k` 次弃权（`k=0,1,…`，
   按操作顺序）的递补者即按同一规则**继续抽第 `名额+k` 轮**的结果（候选池为封存名单
   减去此前全部已出现编号，递补绝不重复）；名单无人可补时席位留空。
   原始中奖顺序与揭晓时间始终不变，弃权只追加记录并改变“当前有效席位”。
7. **奖品发放确认（揭晓后）**：可对当前有人占用的席位登记奖品发放确认，记录**席位、
   当时编号与确认前的弃权次数**。只有该席位的当前有效编号可确认；同一席位同一编号
   重复提交返回原记录（幂等），未揭晓、空席或旧编号（已被递补替换）一律拒绝。
   **确认后该席位不能再弃权**，其他席位仍可照常递补。确认与同席位弃权并发时按提交
   顺序生效（弃权先完成则旧编号确认失败、递补者可确认；确认先完成则弃权失败），
   检查与写入同一事务，不留半次操作。确认记录随核验包导出并由核验器独立重演。

> 安全性前提：操作者在揭晓前对种子保密；承诺与名单摘要由现场监督方事先独立留存。
> 服务器只是“承诺托管板 + 防篡改的名单快照”，种子从不被服务器提前掌握。

---

## 二、一条命令启动（Docker Compose）

前置：已安装 Docker 与 Docker Compose 插件。

```bash
docker compose up -d --build
```

首次会构建镜像并启动；数据保存在命名卷 `lottery-data`（容器内 `/data/lottery.db`），
容器重建后数据仍在。停止：`docker compose down`（加 `-v` 会同时删除数据卷）。

---

## 三、访问地址与端口

| 用途 | 地址 |
| --- | --- |
| 新建轮次 / 轮次列表 | <http://localhost:8000/> |
| 轮次详情（登记/封存/揭晓/导出） | `http://localhost:8000/rounds/<轮次编号>` |
| 浏览器内核验工具 | <http://localhost:8000/verify> |
| 规则文档 | <http://localhost:8000/RULES.md> |

服务监听容器内 **8000** 端口，经 `docker-compose.yml` 映射到宿主机 **8000**。
局域网内其他人访问时，把 `localhost` 换成运行机器的局域网 IP，例如
`http://192.168.1.50:8000/`（需放行防火墙 8000 端口）。
想改宿主机端口，修改 `docker-compose.yml` 中 `"8000:8000"` 的左侧数字即可。

---

## 四、完整操作示例（网页界面）

### 步骤 1：创建轮次（种子不出本机）

1. 打开 <http://localhost:8000/>，填写“轮次名称”和“中奖名额”（例：`年会抽奖`、`3`）。
2. 点击 **“生成种子”**。页面上会出现：
   - 橙色虚线框里的**秘密种子**（64 位十六进制）；
   - 下方的**承诺**（`SHA256("COMMIT:"+种子)`）。
3. 点击 **“复制种子”** 或 **“下载 seed.txt”** 妥善保存（揭晓前保密，丢失则无法揭晓）；
   把**承诺**投影/拍照/抄写给现场人员事先留存。
4. 勾选“我已保存种子并留存承诺”，点击 **“仅提交承诺，创建轮次”**，自动进入轮次页。

### 步骤 2：登记参与者（封存前可改）

- 在“逐个添加”输入编号回车；或在“批量导入”中每行一个编号，点“替换为以上名单”。
- 编号自动去除首尾空白；重复编号会被拒绝。封存前可随时删除或整体替换。
- 页面底部可预览按 UTF-8 字节序排序的规范名单及本地试算摘要。

### 步骤 3：封存名单

- 当人数 ≥ 名额时 **“封存名单”** 按钮才可点击。确认后：
- 页面展示**名单摘要**（64 位十六进制）、封存时间和规范名单快照；
- 名单区消失，服务器此后拒绝添加、删除、替换（尝试修改会返回 409“名单已封存”）。
- 请现场人员核对并留存摘要。

### 步骤 4：揭晓

1. 粘贴事先保存的种子，点击 **“校验承诺并揭晓”**。
2. 种子错误 → 页面提示“种子与事先留存的承诺不匹配，轮次保持封存状态”，
   可修正后重试（失败计数仅作记录，不影响正确揭晓）。
3. 种子正确 → 页面按产生顺序展示中奖编号、揭晓种子与时间；结果永久固定，
   再次提交任何种子都不会重抽。

### 步骤 5：弃权与递补（揭晓后，可选）

1. 结果表按席位展示**原中奖编号**、**当前有效编号**与**递补经过**；
   每个仍有效的席位行尾有 **“弃权”** 按钮。
2. 点击“弃权”并确认后，系统立即按同一公开规则从封存名单中接着选出
   尚未出现的编号补入该席位；名单无人可补时该席位留空（显示“（空席）”）。
3. 原中奖者或递补者都可弃权，可逐次操作多次；“弃权与递补记录”卡片按操作
   顺序列出每次弃权的时间、编号与递补结果。
4. 原始中奖顺序与揭晓时间永远不变；重复提交同一编号的弃权只会返回既有结果，
   不会重复递补。

### 步骤 6：奖品发放确认（揭晓后，可选）

1. 结果表中每个**当前有人占用**、且尚未确认的席位行尾有 **“确认发放”** 按钮；
   点击确认后，系统记录**席位、当时编号与确认前的弃权次数**，行尾显示
   “✅ 已确认发放给 …”，“奖品发放确认记录”卡片列出全部已确认席位。
2. 只有该席位的**当前有效编号**可以确认：空席不能确认；若编号已被递补替换
   （旧编号），确认会被拒绝并提示当前持有者。
3. 同一席位同一编号重复点击只返回既有记录，不会新增。
4. **确认后该席位不能再弃权**（该行的“弃权”按钮不再出现）；其他席位不受影响，
   仍可照常弃权与递补。先弃权再确认也完全合法——记录的“确认前弃权次数”即为
   确认时已发生的弃权次数，核验器据此把确认放回对应时间点重演。

### 步骤 7：导出与核验

点击 **“下载 ZIP 核验包”**（或仅下载 `package.json`），交给任意监督方核验。
核验包附弃权记录（`forfeits`）、当前有效席位（`currentWinners`）与奖品发放确认
记录（`confirmations`）。可在 <http://localhost:8000/verify> 上传 `package.json`
在线核验，或用包内 `verify.py` 命令行核验（见下一节）。

---

## 五、完整操作示例（命令行 / API）

下面用 `curl` 完整走一遍。为方便复现，使用固定演示种子
`0123456789abcdef` 重复 4 次（真实抽签请用浏览器生成的随机种子！）：

```bash
SEED=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
COMMIT=$(python3 -c 'import hashlib,os;print(hashlib.sha256(("COMMIT:"+os.environ["SEED"]).encode()).hexdigest())')
echo "$COMMIT"
# be98cadeb4969ad6bb53c25aa2217fa8b8bf27174e9d7d7a61f963fd6b8cc167
```

```bash
# 1) 创建轮次（只提交承诺）
curl -s -X POST localhost:8000/api/rounds -H 'Content-Type: application/json' \
  -d "{\"title\":\"命令行演示\",\"prizeCount\":3,\"commitment\":\"$COMMIT\"}"
# 记下返回的 "id"，例如 9f2c...
RID=<上一步返回的轮次编号>
```

```bash
# 2) 登记名单（封存前可反复替换）
curl -s -X PUT localhost:8000/api/rounds/$RID/participants \
  -H 'Content-Type: application/json' \
  -d '{"participants":["A001","A002","A003","A004","A005"]}'

# 重复编号会被拒绝：
curl -s -X PUT localhost:8000/api/rounds/$RID/participants \
  -H 'Content-Type: application/json' \
  -d '{"participants":["A001","A001"]}'
# {"ok":false,"error":"编号重复：A001"}
```

```bash
# 3) 封存（人数不足会被拒绝；封存后改名单返回 409）
curl -s -X POST localhost:8000/api/rounds/$RID/seal
# 返回 status=sealed、snapshot、listDigest：
#   listDigest = f798b141dcaca51093500b25f0c442bc95f72343dc84cf42e26bdc629aa2c648

curl -s -X POST localhost:8000/api/rounds/$RID/participants \
  -H 'Content-Type: application/json' -d '{"id":"HACKER"}'
# {"ok":false,"error":"名单已封存，此后拒绝任何修改"}
```

```bash
# 4a) 错误种子：保持封存
curl -s -X POST localhost:8000/api/rounds/$RID/reveal \
  -H 'Content-Type: application/json' -d '{"seed":"ff00...（64位）"}'
# {"ok":false,"commitMismatch":true,...,"round":{"status":"sealed",...}}

# 4b) 正确种子：揭晓
curl -s -X POST localhost:8000/api/rounds/$RID/reveal \
  -H 'Content-Type: application/json' -d "{\"seed\":\"$SEED\"}"
# winners = ["A003","A005","A001"] —— 任何人按规则都能算出同样结果

# 4c) 再次提交：不重抽
curl -s -X POST localhost:8000/api/rounds/$RID/reveal \
  -H 'Content-Type: application/json' -d "{\"seed\":\"$SEED\"}"
# {"ok":true,"alreadyRevealed":true,"message":"该轮次已揭晓，结果固定，重复提交不会重抽",...}
```

```bash
# 5) 弃权与递补（揭晓后）
# 将当前有效中奖编号 A003 标记弃权：立即按规则递补下一位
curl -s -X POST localhost:8000/api/rounds/$RID/forfeit \
  -H 'Content-Type: application/json' -d '{"entry":"A003"}'
# {"ok":true,"forfeit":{"seq":0,"entry":"A003","replacement":"A002"},...}
# round.slots 为当前有效席位：["A002","A005","A001"]；round.forfeits 为弃权记录
# 原始 round.winners（["A003","A005","A001"]）与 round.revealedAt 不变

# 递补者也可再弃权（A002 → 递补 A004）；名单 5 人全部出现后，再弃权则席位留空：
curl -s -X POST localhost:8000/api/rounds/$RID/forfeit \
  -H 'Content-Type: application/json' -d '{"entry":"A002"}'
# {"ok":true,"forfeit":{"seq":1,"entry":"A002","replacement":"A004"},...}

# 重复提交同一弃权：返回既有结果，不重复递补
curl -s -X POST localhost:8000/api/rounds/$RID/forfeit \
  -H 'Content-Type: application/json' -d '{"entry":"A003"}'
# {"ok":true,"alreadyForfeited":true,"message":"该编号已登记弃权，……",...}

# 非当前有效编号（如从未上岗的编号）：不可操作（HTTP 409）
curl -s -X POST localhost:8000/api/rounds/$RID/forfeit \
  -H 'Content-Type: application/json' -d '{"entry":"A006"}'
# {"ok":false,"error":"编号 A006 不是当前有效中奖编号，不能弃权"}
```

```bash
# 6) 奖品发放确认（揭晓后；slot 为席位序号，0 起）
# 当前席位为 ["A002","A005","A001"]：确认席位 0 的当前编号 A002
curl -s -X POST localhost:8000/api/rounds/$RID/confirm \
  -H 'Content-Type: application/json' -d '{"slot":0,"entry":"A002"}'
# {"ok":true,"confirmation":{"slot":0,"entry":"A002","forfeitsBefore":1},...}
# forfeitsBefore=确认前已完成的弃权次数（上面对席位做过 1 次弃权）

# 同一席位同一编号重复提交：返回原记录（alreadyConfirmed），不新增
curl -s -X POST localhost:8000/api/rounds/$RID/confirm \
  -H 'Content-Type: application/json' -d '{"slot":0,"entry":"A002"}'
# {"ok":true,"alreadyConfirmed":true,"message":"该席位该编号已登记奖品发放确认，……",...}

# 旧编号（已被递补替换）确认：拒绝（HTTP 409）
curl -s -X POST localhost:8000/api/rounds/$RID/confirm \
  -H 'Content-Type: application/json' -d '{"slot":0,"entry":"A003"}'
# {"ok":false,"error":"席位 1 已确认发放给编号 A002，编号 A003 是旧编号，不能再确认"}
# （若该席位尚未确认过，则提示“编号 A003 不是席位 1 的当前有效编号（当前为 A002），旧编号不能确认发放”）

# 确认后该席位不能再弃权（HTTP 409）；其他席位照常弃权递补
curl -s -X POST localhost:8000/api/rounds/$RID/forfeit \
  -H 'Content-Type: application/json' -d '{"entry":"A002"}'
# {"ok":false,"error":"席位 1（编号 A002）已登记奖品发放确认，该席位不能再弃权"}

# 空席确认、未揭晓确认、席位序号越界同样被拒（409 / 400）
curl -s -X POST localhost:8000/api/rounds/$RID/confirm \
  -H 'Content-Type: application/json' -d '{"slot":9,"entry":"A001"}'
# {"ok":false,"error":"席位序号越界：共 3 个席位（0 至 2）"}
```

```bash
# 7) 导出核验包
curl -s -OJ localhost:8000/api/rounds/$RID/package.zip
unzip lottery-$RID-package.zip
# 生成目录 lottery-$RID/：package.json  list.txt  RULES.md  verify.py
# package.json 内含 forfeits（弃权记录）、currentWinners（当前有效席位）、
# confirmations（奖品发放确认：席位/当时编号/确认前弃权次数）
```

---

## 六、导出核验包与独立核验

核验包（zip）内容：

| 文件 | 内容 |
| --- | --- |
| `package.json` | 轮次元数据、承诺、种子（未揭晓时为 `null`）、规范名单、名单摘要、规则标识、中奖结果、弃权记录（`forfeits`）、当前有效席位（`currentWinners`）与奖品发放确认（`confirmations`） |
| `list.txt` | 规范名单（每行一个编号） |
| `RULES.md` | 抽奖规则全文（含弃权递补与奖品发放确认规则） |
| `verify.py` | 独立核验脚本，**仅依赖 Python 3 标准库** |

命令行核验（Python 3.8+，无需安装任何东西）：

```bash
python3 verify.py package.json
```

脚本**不相信**包里的 `winners` 字段，而是：

1. 用种子重算 `SHA256("COMMIT:"+种子)` 与承诺比对；
2. 对名单重排、查重并重算规范快照摘要，与包内摘要比对；
3. 检查中奖编号数量、唯一性、是否都在名单内；
4. **从种子和名单按 `sha256-argmax-v1` 重新抽奖**，与包内结果逐项比对；
5. **重演弃权与递补**：第 `k` 次弃权的递补必须等于扩展抽签序列的第 `名额+k` 个
   编号，弃权发生时该编号必须仍在有效席位中，最终当前席位必须与包内
   `currentWinners` 完全一致——伪造或跳过的递补都会被逐条指出；
6. **重演奖品发放确认**：把每条 `confirmations` 记录按其“确认前弃权次数”
   放回弃权序列的对应阶段，核对确认编号正是该阶段该席位的持有者，并确认
   该席位其后没有任何弃权——**伪造编号、错置的确认阶段、确认后的弃权**都会被
   逐条指出；空席确认、同一席位多条确认、阶段越界同样判为不符；
7. 旧版核验包按缺字段兼容：无 `confirmations` 按无确认处理，连弃权字段也没有的
   更早版本按无弃权、无确认处理，照常核验通过；
8. 任何不符逐条打印 `[不符]`，进程以退出码 `1` 结束；全部通过输出“全部核验通过”，退出码 `0`。

结合事先留存的承诺和名单摘要（推荐的真正“外部”核验）：

```bash
python3 verify.py package.json \
  --commitment be98cadeb4969ad6bb53c25aa2217fa8b8bf27174e9d7d7a61f963fd6b8cc167 \
  --list-digest f798b141dcaca51093500b25f0c442bc95f72343dc84cf42e26bdc629aa2c648
```

zip 也可直接传入：`python3 verify.py lottery-<编号>-package.zip`。

对上面命令行演示的固定例子，独立重算结果必然是：

```text
第 1 位 A003
第 2 位 A005
第 3 位 A001
```

若随后发生弃权，扩展抽签序列（同一规则继续往下抽）依次是 `A002`、`A004`：
第 1 次弃权补入 `A002`，第 2 次补入 `A004`，再往后名单无人可补，席位留空。

你可以自行做篡改实验：改 `package.json` 里的一个中奖编号、调换名单顺序、换一个种子、
伪造一条递补或删掉一条弃权记录、把发放确认的编号换成别人、改动确认的
“确认前弃权次数”、或在一条已确认席位之后追加弃权……核验器都会指出具体不符项。

> 封存后、揭晓前也可以导出核验包：此时包内种子与结果为空，核验器核对承诺与名单摘要，
> 并提示“处于封存（未揭晓）状态”。

---

## 七、不用 Docker 的本地运行方式

需要 Python 3.10+：

```bash
pip install -r requirements.txt
# 开发调试
python wsgi.py                 # http://localhost:8000

# 或用 gunicorn（Linux）
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 wsgi:app
```

数据库默认是当前目录下的 `lottery.db`，可用环境变量 `LOTTERY_DB` 修改位置。
gunicorn 建议保持单 worker（SQLite 写串行化即可；多线程没有问题）。

---

## 八、项目结构与接口说明

```
.
├── app/
│   ├── __init__.py        # Flask 应用工厂（静态目录指向仓库根 static/）
│   ├── db.py              # SQLite 连接、建表（含 forfeits 弃权表、confirmations 确认表）、IMMEDIATE 写事务、状态机注释
│   ├── rules.py           # 公开规则的 Python 实现（唯一事实来源之一）
│   ├── services.py        # 名单/封存/揭晓/弃权/发放确认写流程：事务内重新检查状态再读写
│   ├── routes.py          # 页面路由、JSON API、核验包 zip/json 导出
│   └── templates/         # index / round / verify 三个页面（Jinja2）
├── tests/                 # 顺序流程回归 + 弃权递补 + 发放确认 + 确定性交错验证 + HTTP 并发不变量
├── static/
│   ├── css/style.css
│   └── js/
│       ├── sha256.js      # 纯 JS SHA-256（无需安全上下文/CDN）
│       ├── rules.js       # 与 Python 等价的浏览器规则实现（BigInt，含席位重演）
│       ├── common.js      # fetch/转义/复制/下载工具
│       ├── index.js       # 生成种子、只交承诺、轮次列表
│       ├── round.js       # 名单增改、封存、揭晓、弃权递补、发放确认、导出
│       └── verify.js      # 浏览器内核验（独立重算 + 弃权/确认重演）
├── verify/verify.py       # 独立核验脚本（仅 Python 标准库）
├── RULES.md               # 规则全文
├── wsgi.py                # gunicorn wsgi:app
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

状态机：`open`（可改名单）→ `sealed`（快照+摘要固定，等种子）→ `revealed`（结果永久固定，
此后只允许追加弃权与奖品发放确认记录；原始中奖名单与揭晓时间不变，当前有效席位随弃权
递补更新，已确认席位锁定不能再弃权）。

并发与事务：名单新增、删除、整体替换与封存都在同一个 `BEGIN IMMEDIATE` 写事务内
**先重新读取轮次状态与名单、再校验和写入、最后一次提交**。SQLite 的立即写锁使这些
请求互斥串行，因此“封存读取名单之后、提交之前混入新编号”的交错不会发生：

- 名单变更先提交：封存随后在锁内读到变更后的最新名单，按它重新复核人数并保存规范
  快照、人数与摘要（人数不足仍拒绝封存，轮次保持 `open`）；
- 封存先提交：之后的任何名单变更在锁内重新检查时看到 `sealed`，一律返回 409，
  参与者表保持封存时内容，快照与摘要不变；
- 重复封存返回既有快照（`alreadySealed`），不重算、不覆盖。

弃权同理：每次弃权在写事务内重新读取既有弃权记录后追加，同一轮次的并发弃权
串行生效——两次弃权分别取得扩展抽签序列中不同的下一个递补，不会同一人占两个
席位；弃权与递补在同一事务同一行一次写入，不会只记弃权不记递补；
重复提交同一弃权返回既有结果（`alreadyForfeited`），不重复递补。

奖品发放确认与**同席位弃权**也在同一把立即写锁上串行：双方在各自事务内重新读取
弃权记录与确认记录后再检查、写入并一次提交，因此按**提交顺序**生效——

- 弃权先提交：随后的确认在锁内读到递补后的新席位持有者，旧编号确认失败（409），
  递补者仍可凭新编号确认；
- 确认先提交：随后的弃权在锁内读到该席位的确认记录而失败（409）；
- 同一席位同一编号的并发确认恰好一条真正写入，另一条返回既有记录（幂等）。

任何一边校验失败都整体回滚，不会出现“确认写了一半又递补”的半次操作；
已确认席位对其他席位的弃权递补没有任何影响。

测试（仅依赖 Flask，标准库 `unittest`，不改动任何既有接口）：

```bash
pip install -r requirements.txt
python3 -m unittest discover -t . -s tests -v
```

- `tests/test_sequential_flow.py`：按本 README 的命令行示例走完整流程，并用包内
  `verify.py` 独立核验导出的 JSON / ZIP 核验包；
- `tests/test_forfeit.py`：弃权与递补——顺序流程（递补值与规则重算一致、幂等、
  空席、原始结果不变）、核验包重演与篡改检测（伪造/跳过的递补、旧版包兼容）、
  HTTP 并发弃权不变量、确定性交错（后到弃权读到先到记录并取下一个递补）；
- `tests/test_confirm.py`：奖品发放确认——顺序流程（记录席位/编号/确认前弃权次数、
  幂等、旧编号与空席拒绝、确认后席位锁定而其他席位照常递补）、核验包重演与
  篡改检测（伪造编号、错置阶段、确认后的弃权、重复席位、旧版包兼容）、
  HTTP 并发“确认 vs 同席位弃权”恰好一边成功的不变量、两种提交先后的确定性交错；
- `tests/test_interleave.py`：**确定性交错验证**——用两个真实 SQLite 连接手工安排
  新增/整体替换/删除与封存的加锁、阻塞与两种提交先后；
- `tests/test_concurrent_http.py`：多线程真实 HTTP 请求并发新增/替换 + 封存的
  快照—摘要—参与者表一致性不变量检查。

主要 API：

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/rounds` | 创建轮次：`{title, prizeCount, commitment}` |
| `GET  /api/rounds` | 轮次列表 |
| `GET  /api/rounds/<id>` | 轮次详情（含名单/快照/结果/弃权记录/发放确认记录/当前席位） |
| `POST /api/rounds/<id>/participants` | 添加编号（仅 open） |
| `PUT  /api/rounds/<id>/participants` | 整体替换名单（仅 open） |
| `DELETE /api/rounds/<id>/participants/<编号>` | 删除编号（仅 open） |
| `POST /api/rounds/<id>/seal` | 封存（人数 ≥ 名额；幂等） |
| `POST /api/rounds/<id>/reveal` | 提交种子；不匹配保持封存，重复揭晓不重抽 |
| `POST /api/rounds/<id>/forfeit` | 弃权：`{entry}` 须为当前有效编号（仅 revealed）；按规则递补或留空席；重复提交返回既有结果；已确认席位拒绝 |
| `POST /api/rounds/<id>/confirm` | 奖品发放确认：`{slot, entry}`，仅 revealed 且为该席位当前有效编号；记录席位、当时编号、确认前弃权次数；同席位同编号重复提交返回原记录；未揭晓/空席/旧编号拒绝 |
| `GET  /api/rounds/<id>/package.json` | 导出 JSON 核验包（含 `forfeits` / `currentWinners` / `confirmations`） |
| `GET  /api/rounds/<id>/package.zip` | 导出 ZIP 核验包（含规则与核验器） |
