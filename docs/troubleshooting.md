# Troubleshooting

故障排查 cheat sheet。覆盖客户、客服、开发者三种角色。

按"症状 → 可能原因 → 立即可做的检查 → 修复"四列组织。每条都对应一个真实碰到过的问题（不是预测）。

---

## 客户层级（不开终端）

### HUD 黑屏 / 无显示

| 检查 | 修复 |
|---|---|
| USB 线插好了吗（设备端 + 车端） | 重新插紧 |
| USB 线是数据线吗（很多车载 USB 只给电不传数据） | 换数据线 |
| 屏幕背光有没有 | 检查屏幕排线 / 联系开发者 |
| 等了至少 30 秒 | 设备启动需要时间，多等 |

### HUD 显示但 GPS 一直 0 km/h

| 检查 | 修复 |
|---|---|
| 设备能看到天吗（GPS 需要露天） | 移到露天或贴近挡风玻璃 |
| 卫星数（连电脑看 Dashboard） | < 4 颗 fix 不上，等 60-90 秒 |
| 设备装在金属密闭车厢 | 移到外露位置或加外置天线 |

### 限速显示不对

| 现象 | 原因 | 修复 |
|---|---|---|
| 显示了，但限速值跟实际路牌不符 | 数据库覆盖问题（CN 限速仅西安 + 上海高速） | 改路线，或反馈给开发者补数据 |
| 显示 100 km/h 默认值 | GPS 没在数据库覆盖范围内 | 见上 |
| NPU 看到标志但 Dashboard 没反应 | Fusion 需要 4 帧连续相同确认 | 多等一会，或经过同一类标志多次 |

### 双击 launcher 没反应

| 检查 | 修复 |
|---|---|
| .app / .bat 是从 AIHUD U 盘拖出来的吗 | 重新拖一份 |
| **第一次** 是右键打开 / SmartScreen 放行了吗 | 见 `HOW-TO-OPEN.md`，一次性 |
| 浏览器自动开了？地址栏什么 URL | 看下方 "Dashboard 打不开" |

### 浏览器打开但 Dashboard 不显示

| 现象 | 检查 | 修复 |
|---|---|---|
| 显示 "无法连接到 localhost" | adb forward 失败（端口冲突？） | 关掉别的 adb / Android 工具，重新双击 launcher |
| 显示空白 / 一直转圈 | 设备 hud_live.py 没在跑 | 拔 USB + 等 5 秒 + 重插 |
| Dashboard 一直显示 "device offline" 横幅 | 设备掉线或 hud_live.py 重启中 | 等 30 秒，仍不行：拔 USB 重插 |

---

## 客服 / 销售层级（可以指挥客户 + 看截图）

### 客户报 "我的设备不行"

让客户做 **两件事** 就能定位 80% 的问题：

1. 拔下 USB，**再插回电脑**（不是车）
2. 双击 launcher → 截图 Dashboard

看截图：

| Dashboard 看到 | 含义 | 下一步 |
|---|---|---|
| "connected"(绿圆点) + version v0.x.y | 业务正常，是路况 / 操作问题 | 看上方"客户层级" |
| "device offline" 横幅 | 设备 USB 没识别 | 换 USB 线 |
| GPS row "no fix" | 室内无信号或天线问题 | 拿到室外重试 |
| Speed limit "--" 持续 5 秒+ | DB 无覆盖 + NPU 没看到标志 | 正常路况会陆续出值 |
| version 显示 "0.0.0-dev" | 设备从未刷过 OTA | 在 launcher 弹的对话框点 Update |

### 客户问 "怎么升级"

99% 走 OTA：

1. 双击 launcher
2. 弹窗 "New version vX.Y.Z available" → 点 Update
3. 等 30-45 秒
4. 完成

Firmware 升级（季度级，仅在我们告诉客户时才需要）：见 `firmware-update.md`。

---

## 开发者层级（adb + 日志）

### 设备 ADB 连不上

```bash
adb devices
# 如果显示 "no devices/emulators found":
ioreg -p IOUSB -l | grep -iE "rockchip|0x2207"
# 没有 -> USB 物理 / 线问题
# 有 -> 设备在 MaskROM / Loader 模式（idProduct 0x110b / 0x110a）
#       此时只能用 upgrade_tool，不能用 adb
```

### hud_live.py 不在跑

```bash
adb shell 'pidof python3'    # 应该返回 PID
adb shell 'tail -50 /var/log/ai_hud.log | grep -vE "0\.0 km/h"'
# 看最后的错误。常见：
# - import error -> 缺 .py 模块 / .pyc 缓存过期（重启清理）
# - port 80 already in use -> 上一次没清干净（reboot）
# - GPS device not found -> /dev/ttyS4 没枚举（检查 dts）
```

### dashboard 端口 80 不监听

```bash
adb shell 'netstat -ln | grep ":80 "'
# 没有 -> hud_live.py 启动失败前期；看上一条
# 有 -> adb forward 配置问题（host 端）
adb forward --list
adb forward tcp:8080 tcp:80
```

### NPU 不出检测 / 一直 0 confidence

```bash
adb shell 'cat /tmp/ai_hud_detect'
# 应该看到 speed_limit, camera, confidence, timestamp
# 全 0 -> 模型加载失败或 NPU 驱动 hang，看 ai_hud_c.log
adb shell 'tail -100 /var/log/ai_hud_c.log | grep -iE "error|fatal|fail"'
```

### 历史踩坑速查

| 现象 | 根因 | 修复 commit / 文档 |
|---|---|---|
| "屏幕无标志却看到 80@40% 误检" | BOX_THRESH 0.40 < 训练 F1 最优 0.664 | 84767fa: 提到 0.65 |
| GT911 触摸 reset 后冻结 | 频繁 hw reset 永久损坏 GT911 | MEMORY `_STALL_COOLDOWN_S=30.0` |
| init.d 启动后 LD_LIBRARY_PATH 丢失 | Buildroot init.d 不继承 user profile | S99_ai_hud 显式 export |
| `(sleep 2; reboot) &` 没生效 | adb shell SIGHUP 杀子进程 | OTA 改用直接 `reboot` |
| 64 MB FAT32 newfs_msdos 失败 | cluster 数差 500 个 | `-c 1` 替代 `-c 2` |
| Windows launcher .bat 双击没反应 | execution policy / SmartScreen | 使用 .bat 包装 -ExecutionPolicy Bypass + 文档提醒 |
| dashboard 显示 DB built date "unknown" | v1 (16-byte) header 没 build_epoch | prepare_speed_db.py 升 v2 |
| OTA 中 service restart 后进程消失 | nohup 不抵 SIGHUP | post_deploy 改用 `reboot` |
| Firmware build CI 不识别 NCM | kernel defconfig 缺 CONFIG_USB_CONFIGFS_NCM | sdk-build.yml patch step |
| Web Dashboard 2s 轮询每次刷 DOM | 没做 state diff | `_stateSig` 比对 + skip render |
| Speed DB 无 fix 区域 fusion 失效 | `confidence_no_db=0.60 < BOX_THRESH=0.65` | 调到 0.75 |

### 出厂前 / 重大改动后

跑 `tools/factory_test.sh` -- 12 项 sanity check，3 秒出结果。

### 设备完全 brick 救援

唯一兜底：**MaskROM 模式**

1. 拔 USB
2. 按住 BOOT 按钮（不松手）
3. 重新插 USB
4. 松开 BOOT
5. `sudo upgrade_tool LD` 看到 `Maskrom Device #1` 即成功
6. `sudo upgrade_tool UF /path/to/update.img` 整刷出厂或最新 firmware

任何时候只要 SoC 硅没坏，MaskROM 都能用。**不可能软件砖**。

### 远程协助客户

如果客户能拷贝粘贴 + 跟着做：

```bash
# 让客户在 terminal 跑（mac）：
xattr -d com.apple.quarantine "/Applications/AI-HUD Config.app" 2>/dev/null || true
# 如果 launcher 一直打不开 / Gatekeeper 卡死
```

如果客户在 Windows：

```cmd
# 跑 launcher 之前在 PowerShell：
Set-MpPreference -DisableRealtimeMonitoring $false
# 偶尔杀软会把 adb.exe 误杀，重新启用 Defender 反而能让 adb 正常运行
```

---

## 升级 / OTA 失败

| 现象 | 修复 |
|---|---|
| launcher 卡在"downloading" | 切到 mirrors.conf 里的 Gitee/OSS 备份 |
| 下载完报 "checksum mismatch" | 网络坏，删 `~/Library/Caches/AI-HUD/` 重试 |
| push 一半 adb 断开 | 拔插 USB 重试，OTA 是幂等的 |
| reboot 后 launcher 探测超时 | 等 60 秒手动重新双击 |
| firmware flash 中途 USB 掉 | 重新按 BOOT + 重插 USB + 重试 upgrade_tool UF |

---

## 看在哪

| 想看什么 | 在哪 |
|---|---|
| 系统级日志 | `adb shell tail -f /var/log/ai_hud.log` |
| C-side 日志（NPU / 摄像头） | `adb shell tail -f /var/log/ai_hud_c.log` |
| 当前 fusion 决策 | Dashboard "Live status" 段 |
| 当前版本 | `adb shell cat /root/version.txt` 或 Dashboard 底部 |
| DB 新鲜度 | Dashboard "Speed DB" row "built" 日期 |
| 出厂检查清单 | `bash tools/factory_test.sh` |
| 客户经历的 USB 状态 | `ioreg -p IOUSB -l \| grep rockchip` |
