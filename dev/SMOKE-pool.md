# Context 池冒烟验收手册（Task 7）

> 计划：`docs/superpowers/plans/2026-10-24-context-pool-captcha-retry-memflags.md` Task 7
> 前置：Task 1–6 及终审修复已全部合入（802880d、cb35621 为止），全量测试 80 passed / 1 环境性失败
> 本步骤需要两个真实已登录的豆包账号，无法自动化——由使用者执行，把观察结果填写在每项的「实测」栏。

## 准备

1. 在 `http://127.0.0.1:9090/admin` 的「账号池管理」中准备**两个**账号（扫码登录一个 + 「保存当前账号」，再导入另一账号的 Cookie，或两次扫码）。
2. 以池模式启动服务（Windows）：

```bat
set DOUBAO_POOL_SIZE=2
.venv\Scripts\python.exe -m doubao2api
```

（或你惯用的启动脚本，带上 `DOUBAO_POOL_SIZE=2` 环境变量。）

## 验收点

### 1. 双账号 context 建立

发第一个请求后观察服务日志：

- [ ] 出现 `Service context ready for account <id> (<name>), pool=1/2`
- [ ] 换第二个新会话再发，出现另一账号的 `pool=2/2` 日志
- 实测：＿＿＿＿

### 2. 并行（真·多账号并发）

两个终端同时（间隔 < 1 秒）发流式请求：

```bash
curl -N -X POST http://127.0.0.1:9090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-2.1-turbo","stream":true,"messages":[{"role":"user","content":"从1数到10"}]}'
```

- [ ] 第二条的 TTFT 不因第一条未完成而明显等待（对比单页时代的串行行为）
- [ ] 两条流各自完整收到 `[DONE]`
- 实测：TTFT-1=____s TTFT-2=____s（大致即可）

### 3. 会话粘性

用第 2 步任一响应里的 `conversation_id` 追问：

```bash
curl -N -X POST http://127.0.0.1:9090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-2.1-turbo","stream":true,"conversation_id":"<上一步的id>","messages":[{"role":"user","content":"继续"}]}'
```

- [ ] 服务日志显示该请求路由到了与首轮相同的账号（或无 `Service context ready` 新建日志=复用）
- 实测：＿＿＿＿

### 4. 内存增量

任务管理器对比 `DOUBAO_POOL_SIZE=1` 与 `=2` 的整棵进程树内存：

- [ ] 记录两个数字（预期每多一个 context 增量约 100–200MB）
- 实测：1 池=____MB，2 池=____MB

### 5. 验证码逃生（可选，风险自负）

短时间内高频调用视频生成人为触发风控（`710022002/710022004`），观察：

- [ ] 日志出现 `Service context released for account <id>`（风控账号 context 报废）
- [ ] 后续聊天请求无感切到另一账号，**无 reload 日志**、无在途流中断
- [ ] `/admin` 中该账号状态变为 captcha，Tier-2 自动滑块求解先尝试了 2 次（`attempt 1/2` 日志）
- 实测：＿＿＿＿

### 6. 池模式视频额度（终审遗留验证点）

让当前路由账号的视频额度耗尽后发起视频请求：

- [ ] 返回 502（`generate_video quota exceeded (pool account ...)`），**不是**级联误标所有账号
- [ ] `/admin` 里被标记 quota_exceeded 的是**路由账号**，不是默认账号
- 注：池内自动换号重试是记录在案的已知延后项——此场景第一版返回 502 是正确行为
- 实测：＿＿＿＿

## 结果回填后

把本文件提交入库（`git add dev/SMOKE-pool.md && git commit -m "docs: context 池冒烟验收记录"`），即可宣告计划全部完成。任何一项不符，对照日志在仓库提 issue 或直接修。
