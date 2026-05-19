# Luckfox Pico Ultra RV1106 完整技术调研报告

> 调研日期: 2026-05-07
> 数据来源: https://wiki.luckfox.com/zh/Luckfox-Pico-Ultra 官方Wiki

---

## 目录

1. [产品概述与硬件规格](#1-产品概述与硬件规格)
2. [镜像烧录指南](#2-镜像烧录指南)
3. [登录与连接方式](#3-登录与连接方式)
4. [网络配置](#4-网络配置)
5. [SDK编译与开发环境](#5-sdk编译与开发环境)
6. [交叉编译](#6-交叉编译)
7. [外设与接口](#7-外设与接口)
8. [显示屏(RGB Screen)](#8-显示屏rgb-screen)
9. [摄像头(CSI Camera)](#9-摄像头csi-camera)
10. [NPU/RKNN 人工智能推理](#10-npurknn-人工智能推理)
11. [RKMPI 多媒体处理](#11-rkmpi-多媒体处理)
12. [音频系统](#12-音频系统)
13. [WiFi与蓝牙](#13-wifi与蓝牙)
14. [文件传输](#14-文件传输)
15. [自启动与静态IP](#15-自启动与静态ip)
16. [Linux基础与分区信息](#16-linux基础与分区信息)

---

## 1. 产品概述与硬件规格

### 1.1 芯片介绍

Luckfox Pico Ultra 系列基于瑞芯微 RV1106 芯片，这是一款专门用于人工智能相关应用的高度集成 IPC 视觉处理器 SoC。处理器采用单核 ARM Cortex-A7 32 位内核，集成了 NEON 和 FPU。

内置全新基于硬件的 ISP，支持多种算法加速器，包括 HDR、3A、LSC、3DNR、2DNR、锐化、去雾、伽马校正。

内置 16 位 DRAM DDR3L、POR、音频编解码器和 MAC PHY。全系列开发板支持 Buildroot 系统。

### 1.2 型号对比规格表

| 规格项 | Pico Ultra | Pico Ultra W | Pico Ultra B | Pico Ultra BW |
|--------|-----------|-------------|------------|--------------|
| **芯片** | RV1106G3 | RV1106G2 | RV1106G3 | RV1106G2 |
| **处理器** | Cortex A7 @ 1.2GHz | Cortex A7 @ 1.2GHz | Cortex A7 @ 1.2GHz | Cortex A7 @ 1.2GHz |
| **NPU** | 1 TOPS (int4/8/16) | 0.5 TOPS (int4/8/16) | 1 TOPS (int4/8/16) | 0.5 TOPS (int4/8/16) |
| **ISP** | 最大输入 5M @30fps | 最大输入 5M @30fps | 最大输入 5M @30fps | 最大输入 5M @30fps |
| **内存** | 256MB DDR3L | 128MB DDR3L | 256MB DDR3L | 128MB DDR3L |
| **Wi-Fi/蓝牙** | 无 | 2.4GHz WiFi6, BT5.2/BLE | 无 | 2.4GHz WiFi6, BT5.2/BLE |
| **摄像头接口** | MIPI CSI 2-lane | MIPI CSI 2-lane | MIPI CSI 2-lane | MIPI CSI 2-lane |
| **DPI接口** | RGB666 | RGB666 | RGB666 | RGB666 |
| **POE接口** | IEEE 802.3af PoE | IEEE 802.3af PoE | IEEE 802.3af PoE | IEEE 802.3af PoE |
| **喇叭接口** | MX1.25mm | MX1.25mm | MX1.25mm | MX1.25mm |
| **USB** | USB 2.0 Host/Device | USB 2.0 Host/Device | USB 2.0 Host/Device | USB 2.0 Host/Device |
| **GPIO** | 30个引脚 | 30个引脚 | 30个引脚 | 30个引脚 |
| **网口** | 10/100M Ethernet | 10/100M Ethernet | 10/100M Ethernet | 10/100M Ethernet |
| **存储** | eMMC 8GB | eMMC 8GB | eMMC 8GB | eMMC 8GB |

**关键差异说明:**
- G3 芯片 (Ultra/Ultra B): NPU 算力 1 TOPS, 内存 256MB
- G2 芯片 (Ultra W/Ultra BW): NPU 算力 0.5 TOPS, 内存 128MB, 但带 WiFi6 + BT5.2
- W 后缀: 带无线功能 (WiFi + 蓝牙)
- B 后缀: 目前规格与无B版本相同（可能为外观/接口布局差异）

### 1.3 PoE 模块

- 标准: IEEE 802.3af PoE
- 输入电压: 37V ~ 57V DC
- 输出电压: 5V 2.5A DC
- 尺寸: 50.00 x 34.80mm
- 需配合支持 802.3af 的 PoE 交换机/路由器使用

---

## 2. 镜像烧录指南

### 2.1 概述

Luckfox Pico Ultra 系列内置 eMMC，出厂预置工厂测试镜像。用户需自行烧录操作系统以确保 luckfox-config、adb 等功能正常运行。

### 2.2 镜像下载

**官方 Buildroot 镜像:**
- Pico Ultra / Ultra B: `Luckfox_Pico_Ultra_EMMC_250313`
- Pico Ultra W / Ultra BW: `Luckfox_Pico_Ultra_W_EMMC_250313`

**第三方 Ubuntu 22.04 镜像:**
- `Ubuntu_Luckfox_Pico_Ultra_EMMC_250313`

注意: 日期后缀 (250313) 表示版本，请始终下载最新日期的镜像。

### 2.3 Windows 烧录

#### 驱动安装
1. 下载 RK 驱动助手 DriverAssitant
2. 打开工具安装 USB 驱动程序
3. 安装完成后重启电脑

#### 烧录工具
- 工具: SocToolKit (v1.98_20240705_01_win 版本)
- 以管理员身份运行
- 选择 RV1106 设备

#### 方式一: 分区镜像烧录
1. 按住 BOOT 键同时用 USB 连接电脑
2. 等待显示 MaskRom 设备后松开 BOOT 键
3. 点击 Search Path 选择固件目录
4. 勾选需要烧录的分区项
5. 点击 Download 按钮执行烧录

#### 方式二: 完整镜像一键烧录
1. 按住 BOOT 键同时用 USB 连接电脑
2. 等待显示 MaskRom 设备后松开 BOOT 键
3. 点击 Firmware 选择固件目录
4. 点击 Upgrade 按钮执行烧录

### 2.4 Linux 烧录 (仅支持 Ubuntu 22.04 x86_64)

#### 安装 upgrade_tool
```bash
sudo unzip upgrade_tool_v2.17.zip
cd upgrade_tool_v2.17_for_linux/
sudo cp upgrade_tool /usr/local/bin
sudo chmod +x /usr/local/bin/upgrade_tool
```

#### 验证安装
```bash
sudo upgrade_tool -v
# 输出: Upgrade Tool v2.17
```

#### 进入烧录模式
按住 BOOT 按键同时连接主机。使用 `lsusb` 验证设备识别。

#### 执行烧录
```bash
sudo upgrade_tool uf update.img
```

#### 使用 SDK 脚本一键烧录
前提: output/image 目录下必须存在 update.img 文件
```bash
sudo ./rkflash.sh update
```

### 2.5 macOS 烧录

| 系统版本 | 工具版本 |
|---------|--------|
| macOS 12.7 | upgrade_tool_v2.25_for_mac.zip |
| macOS 26.1 | upgrade_tool_v2.44_for_mac.zip |

```bash
cd upgrade_tool_v2.44_mac
sudo ./upgrade_tool uf Luckfox-xxx-xxx.img
```

### 2.6 快速进入烧录模式
若已通过 ADB 连接，可执行 `reboot loader` 命令快速进入烧录模式。

---

## 3. 登录与连接方式

### 3.1 默认凭证

| 系统 | 用户名 | 密码 | USB静态IP |
|------|--------|------|-----------|
| Buildroot | root | luckfox | 172.32.0.93 |
| Ubuntu 22.04 | pico | luckfox | 172.32.0.70 |

### 3.2 ADB 登录

#### Windows
1. 下载 ADB 工具包并解压
2. 配置系统环境变量，添加 ADB 解压路径
3. 登录命令:
```bash
adb shell
```
4. 多设备场景:
```bash
adb devices
adb -s [设备序列号] shell
```

#### Ubuntu
```bash
sudo apt-get install android-tools-adb -y
```

配置 udev 规则:
```bash
sudo vim /etc/udev/rules.d/51-android.rules
```
添加:
```
SUBSYSTEM=="usb", ATTR{idVendor}=="2207", ATTR{idProduct}=="0019", MODE="0666", GROUP="plugdev"
```
重载规则:
```bash
sudo chmod 644 /etc/udev/rules.d/51-android.rules
sudo udevadm control --reload-rules
sudo service udev restart
```

### 3.3 SSH 登录

#### Windows
1. 关闭 Windows 防火墙
2. 配置 RNDIS 网卡 IPv4 地址为 172.32.0.100
3. 使用 Zenmap 扫描网段发现设备 IP
4. 使用 MobaXterm: Session -> SSH -> 输入设备 IP 和凭证

#### Ubuntu
```bash
# 验证 RNDIS
sudo dmesg | tail -n 50

# 查看网卡接口
ifconfig

# 配置 RNDIS
sudo ip addr add 172.32.0.98/24 dev [网卡名称]
sudo ip link set [网卡名称] up
```

### 3.4 串口登录

- 接线: TX、GND、RX 连接到 USB 转 TTL 模块
- 工具: MobaXterm
- 波特率: 115200
- Session -> Serial -> 选择相应 COM 口

**注意:** 输入密码时屏幕无显示，这是正常现象。

---

## 4. 网络配置

### 4.1 离线通信(网线直连)

#### 开发板端配置
```bash
ifconfig eth0 192.168.10.200 netmask 255.255.252.0
route add default gw 192.168.11.1
echo "nameserver 114.114.114.114" > /etc/resolv.conf
```

#### 自启动静态IP脚本
创建 `/etc/init.d/S99eth0_staticip`:
```bash
#!/bin/sh
case $1 in
  start)
    ifconfig eth0 192.168.10.200 netmask 255.255.252.0
    route add default gw 192.168.11.1
    echo "nameserver 114.114.114.114" > /etc/resolv.conf
    ;;
  stop)
    ;;
  *)
    exit 1
    ;;
esac
```

#### Windows 端配置
将以太网 IPv4 设置为: IP 192.168.10.x, 子网掩码 255.255.252.0

### 4.2 在线通信(网络桥接)
1. 选择无线网络和以太网进行桥接
2. 以太网自动分配 IP 192.168.137.1 (不可手动修改)
3. DNS 需手动配置

---

## 5. SDK 编译与开发环境

### 5.1 SDK 获取

```bash
# Gitee (国内推荐)
git clone https://gitee.com/LuckfoxTECH/luckfox-pico.git

# GitHub
git clone https://github.com/LuckfoxTECH/luckfox-pico.git
```

**系统要求:** 仅支持 Ubuntu 22.04 x86_64 环境

### 5.2 SDK 目录结构

```
luckfox-pico/
├── build.sh -> project/build.sh    # 编译脚本
├── media/                           # 多媒体编解码、ISP算法
├── sysdrv/                          # U-Boot、kernel、rootfs
├── project/                         # 参考应用、编译配置
├── output/                          # 编译后镜像输出
└── tools/                           # 烧录工具
```

输出镜像文件: `download.bin`, `env.img`, `uboot.img`, `idblock.img`, `boot.img`, `rootfs.img`, `userdata.img`

### 5.3 安装依赖

```bash
sudo apt update
sudo apt-get install -y git ssh make gcc gcc-multilib g++-multilib \
  module-assistant expect g++ gawk texinfo libssl-dev bison flex \
  fakeroot cmake unzip gperf autoconf device-tree-compiler \
  libncurses5-dev pkg-config bc python-is-python3 passwd openssl \
  openssh-server openssh-client vim file cpio rsync curl
```

### 5.4 编译步骤

#### 选择硬件版本
```bash
./build.sh lunch
```
交互选择:
- 选项 6: RV1106_Luckfox_Pico_Pi
- 启动媒介: 选项 0 (EMMC)
- 系统版本: 选项 0 (Buildroot)

#### 执行编译
```bash
./build.sh
```

### 5.5 常用编译命令

```bash
# 单独编译内核
./build.sh clean kernel
./build.sh kernel

# 单独编译 U-Boot
./build.sh clean uboot
./build.sh uboot

# 单独编译 rootfs
./build.sh clean rootfs
./build.sh rootfs

# 固件打包(含自定义文件)
./build.sh firmware
```

### 5.6 BoardConfig 关键配置参数

```bash
RK_BOOTARGS_CMA_SIZE="66M"           # 摄像头内存分配
RK_KERNEL_DTS=rv1106g-luckfox-pico-pro-max.dts  # 设备树
RK_BOOT_MEDIUM=sd_card                # 启动介质: sd_card/spi_nand/eMMC
LF_TARGET_ROOTFS=buildroot            # 根文件系统
RK_BUILDROOT_DEFCONFIG=luckfox_pico_defconfig   # Buildroot配置
RK_POST_OVERLAY="overlay-luckfox-config..."     # 打包文件目录

# 分区配置
RK_PARTITION_CMD_IN_ENV="32K(env),512K@32K(idblock),256K(uboot),32M(boot),512M(oem),256M(userdata),6G(rootfs)"
```

### 5.7 自定义文件打包 (Overlay 机制)

1. 在 `project/cfg/BoardConfig_IPC/overlay` 创建自定义文件夹:
```
custom-overlay/
└── etc
    ├── samba
    │   └── smb.conf
    ├── shadow
    └── ssh
        └── sshd_config
```

2. 在 BoardConfig.mk 中配置:
```bash
export RK_POST_OVERLAY="custom-overlay"
```

3. 编译打包:
```bash
./build.sh firmware
```

### 5.8 常见问题

**WSL2 编译路径错误:**
```bash
export PATH=$(echo "$PATH" | tr -d ' \t\n')
```

**Buildroot 下载失败:** 使用离线包替换 dl 文件夹:
```bash
tar -xjvf dl.tar.bz2 -C luckfox-pico/sysdrv/source/buildroot/buildroot-2023.02.6/
```

**重要提示:** 请勿在镜像编译过程中滥用 sudo 命令，否则可能造成文件权限变更。

---

## 6. 交叉编译

### 6.1 工具链选择

| 系统类型 | 工具链 | 用途 |
|---------|--------|------|
| Buildroot (uclibc) | arm-rockchip830-linux-uclibcgnueabihf | Buildroot 系统编译 |
| Ubuntu (glibc) | gcc-arm-11.2-2022.02-x86_64-arm-none-linux-gnueabihf | Ubuntu 系统编译 |

### 6.2 Buildroot 编译流程

```bash
# 解压工具链
tar zxvf arm-rockchip830-linux-uclibcgnueabihf.tar.gz -C ~/

# 设置 PATH (添加到 ~/.bashrc)
export PATH=~/arm-rockchip830-linux-uclibcgnueabihf/bin:$PATH

# 编译
arm-rockchip830-linux-uclibcgnueabihf-gcc hello.c -o hello

# 传输到开发板
scp hello root@192.168.9.128:/root
```

### 6.3 Ubuntu 编译流程

```bash
# 编译
arm-none-linux-gnueabihf-gcc hello.c -o hello

# 传输到开发板
scp hello pico@192.168.9.128:/home/pico
```

### 6.4 Makefile 示例 (Buildroot)

```makefile
CC := /home/buildroot/luckfox-pico/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin/arm-rockchip830-linux-uclibcgnueabihf-gcc

hello: hello.c
	$(CC) $^ -o $@
```

---

## 7. 外设与接口

### 7.1 GPIO

#### 引脚编号系统
GPIO 共 5 个 bank (GPIO0~GPIO4)，每个 bank 分 4 组 (A/B/C/D)，共 32 个 pin。

**计算公式:**
```
pin = bank x 32 + (group x 8 + X)
```
示例: GPIO1_B1_d = 1 x 32 + (1 x 8 + 1) = 41

#### Shell 命令控制
```bash
# 导出引脚
echo 41 > /sys/class/gpio/export

# 设置为输出
echo out > /sys/class/gpio/gpio41/direction

# 输出高电平
echo 1 > /sys/class/gpio/gpio41/value

# 输出低电平
echo 0 > /sys/class/gpio/gpio41/value

# 读取状态
cat /sys/class/gpio/gpio41/value

# 取消导出
echo 41 > /sys/class/gpio/unexport
```

#### GPIO 属性文件
- `direction`: in (输入) / out (输出)
- `value`: 0 (低电平) / 1 (高电平)
- `edge`: rising / falling / both / none (中断配置)

#### Python 实现
```python
from periphery import GPIO

Write_GPIO = GPIO(41, "out")
Read_GPIO = GPIO(40, "in")

Write_GPIO.write(True)   # 输出高电平
pin_state = Read_GPIO.read()  # 读取引脚状态
```

### 7.2 PWM

#### 启用 PWM
```bash
luckfox-config
# -> Advanced Options -> PWM -> 选择接口 (如 PWM7_M1) -> Enable -> Reboot
```

#### Shell 控制
```bash
# 导出通道
echo 0 > /sys/class/pwm/pwmchip10/export

# 设置 1kHz 频率 (周期 1000000 纳秒)
echo 1000000 > /sys/class/pwm/pwmchip10/pwm0/period

# 设置占空比 50%
echo 500000 > /sys/class/pwm/pwmchip10/pwm0/duty_cycle

# 设置极性
echo "normal" > /sys/class/pwm/pwmchip1/pwm0/polarity

# 启用输出
echo 1 > /sys/class/pwm/pwmchip1/pwm0/enable
```

### 7.3 UART

- UART2 为调试串口 (默认开启)，其他串口默认未开启
- 设备文件: `/dev/ttyS[n]`

#### Shell 配置
```bash
stty -F /dev/ttyS3 ispeed 115200 ospeed 115200
stty -F /dev/ttyS3 -echo
```

#### Python 实现
```python
import serial

with serial.Serial("/dev/ttyS3", baudrate=115200,
    bytesize=serial.EIGHTBITS, stopbits=serial.STOPBITS_ONE,
    parity=serial.PARITY_NONE, timeout=1) as uart3:
    uart3.write(b"Hello World!\n")
    buf = uart3.read(128)
```

### 7.4 I2C

设备路径: `/dev/i2c-3`

#### Shell 测试命令
```bash
# 检测设备
i2cdetect -a -y 3

# 读取所有寄存器
i2cdump -f -y 3 0x68

# 读取特定寄存器
i2cget -f -y 3 0x68 0x01

# 写入寄存器
i2cset -f -y 3 0x68 0x01 0x6f
```

#### Python 实现
```python
import smbus

i2c_bus = smbus.SMBus(3)
i2c_bus.write_i2c_block_data(address, 0, data)
```

### 7.5 SPI

设备文件: `/dev/spidevX.Y` (X=总线号, Y=设备号)

#### Python 实现
```python
import spidev

spi = spidev.SpiDev()
spi.open(0, 0)          # Bus 0, Device 0
spi.max_speed_hz = 1000000  # 1 MHz

tx_data = [ord(c) for c in "hello world!"]
rx_data = spi.xfer2(tx_data[:])
spi.close()
```

#### C 实现关键 ioctl 参数
- `SPI_IOC_WR_MODE`: 配置时钟极性/相位
- `SPI_IOC_WR_BITS_PER_WORD`: 设置字长 (通常 8)
- `SPI_IOC_WR_MAX_SPEED_HZ`: 设置总线速度
- `SPI_IOC_MESSAGE(N)`: 执行传输

### 7.6 ADC

- 电压范围: 0V ~ 1.8V
- 引脚: 144, 145
- 设备文件: `/sys/bus/iio/devices/iio:device0`

#### 读取 ADC 值
```bash
cat /sys/bus/iio/devices/iio:device0/in_voltage0_raw
cat /sys/bus/iio/devices/iio:device0/in_voltage_scale
```

#### 电压计算公式
```
voltage = (raw_value x scale) / 1000
```

#### Python 实现
```python
import time

ADC_DIR = "/sys/bus/iio/devices/iio:device0"

def read_value(file_path):
    with open(file_path, "r") as file:
        return file.read().strip()

while True:
    scale = float(read_value(f"{ADC_DIR}/in_voltage_scale"))
    raw0 = float(read_value(f"{ADC_DIR}/in_voltage0_raw"))
    voltage = f"{raw0 * scale / 1000:.2f}"
    print(f"IN0_Voltage: {voltage} V")
    time.sleep(1)
```

### 7.7 USB

- 同时提供 USB-A 和 USB-C 接口
- 内置切换芯片: USB-C 有供电时切换到 USB-C，无供电时切换到 USB-A
- 通过 `luckfox-config` -> Advanced Options -> USB 切换 HOST 模式

#### USB 存储设备
```bash
mount -t vfat /dev/sdb1 /mnt/sdcard/
ls /mnt/sdcard/
```

#### USB 摄像头
```bash
# 拍照
fswebcam -d /dev/video0 -r 1920x1080 output.jpg
```

### 7.8 RTC

RV1106 内置 RTC 模块 (RV1103 无内置 RTC)。

#### 读取/设置时间
```bash
# 读取 RTC 时间
hwclock --show

# 系统时间同步到 RTC
hwclock --systohc

# RTC 时间同步到系统
hwclock --hctosys
```

#### NTP 网络时间同步
```bash
ntpd -p cn.ntp.org.cn -qn
```

#### 时区配置
在 `/etc/profile` 中添加:
```bash
export TZ=CST-8
```

#### 启动时同步
```bash
hwclock -u -s
```

---

## 8. 显示屏(RGB Screen)

### 8.1 支持的屏幕

| 型号 | 分辨率 |
|------|--------|
| LF40-720720-ARK | 720 x 720 |
| LF40-480480-ARK | 480 x 480 |

接口类型: RGB LCD 并行接口，数据以 RGB666 格式传输，每个像素占用 6 位。

### 8.2 DPI 接口信号线

| 信号 | 功能 |
|------|------|
| DPIVSYNC | 垂直同步，指示帧开始 |
| DPIHSYNC | 水平同步，指示行开始 |
| DPIDE | 数据有效信号 |
| DPICK | 像素时钟信号 |
| 数据线 | 并行数据传输 (RGB666) |

### 8.3 内核配置

必需开启的配置项:
- `CONFIG_DRM_ROCKCHIP`
- `CONFIG_ROCKCHIP_VOP` (Rockchip VOP driver)
- `CONFIG_ROCKCHIP_RGB` (Rockchip RGB support)

```bash
./build.sh kernelconfig   # Buildroot
sudo ./build.sh kernelconfig  # Ubuntu
./build.sh kernel         # 编译生成 boot.img
```

### 8.4 设备树配置

#### LF40-720720-ARK 时序参数
```
clock-frequency = <30000000>
hactive = <720>, vactive = <720>
hback-porch = <44>, hfront-porch = <46>
vback-porch = <18>, vfront-porch = <16>
hsync-len = <2>, vsync-len = <2>
```

#### LF40-480480-ARK 时序参数
```
clock-frequency = <16500000>
hactive = <480>, vactive = <480>
hback-porch = <10>, hfront-porch = <50>
vback-porch = <8>, vfront-porch = <8>
hsync-len = <4>, vsync-len = <10>
pixelclk-active = <1>
```

#### CMA 内存配置
720x720 分辨率需额外申请 10MB 内存作为 CMA。不使用时可注释 linux,cma 节点释放资源。

### 8.5 DRM 框架核心概念

| 概念 | 说明 |
|------|------|
| CRTC | 显示控制器，对应 VOP 模块 |
| Plane | 图层，VOP 的 win 图层抽象 |
| Encoder | RGB、LVDS、DSI 等接口转换器 |
| Connector | Encoder 和 Panel 间的接口 |
| Panel | LCD 显示设备抽象 |

### 8.6 DRM 测试命令

```bash
# 获取 Connector ID 和 CRTC ID
modetest -M rockchip

# 显示测试
modetest -M rockchip -s 70@66:480x480   # 480x480
modetest -M rockchip -s 70@66:720x720   # 720x720
```

### 8.7 LVGL 示例程序

```bash
# 获取源码
git clone https://github.com/LuckfoxTECH/luckfox_pico_lvgl_example.git

# 编译
mkdir build && cd build
export LUCKFOX_SDK_PATH=<SDK路径>
cmake .. && make -j

# 运行
RkLunch-stop.sh    # 停止默认 RKIPC 程序
chmod a+x ./luckfox_lvgl_demo
./luckfox_lvgl_demo
```

### 8.8 电容触摸屏 (GT911)

内核需开启 `TOUCHSCREEN_GOODIX` 驱动，GT911 连接 I2C3，地址 0x14。

```bash
# I2C 检测 (0x14 显示 "UU" 表示已识别)
i2cdetect -y 3

# 触摸测试
hexdump /dev/input/event0
evtest   # 查看 ABS_X 和 ABS_Y 坐标数据
```

**注意:** GT911 触摸屏会占用 I2C3M2，需确保其他组未被使用。

### 8.9 驱动源码位置

```
drivers/gpu/drm/rockchip/rockchip_drm_drv.c   # DRM 核心
drivers/gpu/drm/rockchip/rockchip_drm_vop.c   # VOP 驱动
drivers/gpu/drm/rockchip/rockchip_rgb.c        # RGB 驱动
drivers/gpu/drm/panel/panel-simple.c            # Panel 驱动
```

---

## 9. 摄像头(CSI Camera)

### 9.1 支持的传感器

| 型号 | 像素 | 特点 |
|------|------|------|
| SC3336 3MP Camera (A) | 300万 | 高灵敏度、高信噪比、低照度 |
| MIS5001 | 500万 | 广角/广角无畸变版本，仅支持 RV1106 |

### 9.2 硬件连接
排线金属面朝向开发板芯片方向插入。

### 9.3 RTSP 推流
使用 VLC 播放器输入地址: `rtsp://172.32.0.93/live/0`

### 9.4 V4L2 测试命令

```bash
# 列出设备
v4l2-ctl --list-devices

# 列出格式
v4l2-ctl --device=/dev/video15 --list-formats-ext

# 录制视频
v4l2-ctl --device=/dev/video15 \
  --set-fmt-video=width=640,height=480,pixelformat=NV12 \
  --stream-mmap --stream-to=video50.yuv --stream-count=30
```

### 9.5 视频回放
```bash
ffplay -video_size 640x480 -pixel_format nv12 -framerate 10 -i video50.yuv
```

### 9.6 UVC 模拟
- 快速启动模式: 执行 `usb_config.sh` 后启动 UVC
- 标准模式: 在 SDK 中添加 `UVC_TINY` 配置

---

## 10. NPU/RKNN 人工智能推理

### 10.1 概述

瑞芯微第4代 NPU (RKNPU)，支持 int4/int8/int16 混合量化。RV1106G3 提供 1 TOPS 算力，RV1106G2 提供 0.5 TOPS 算力。

### 10.2 RKNN-Toolkit2 安装

#### 本地安装 (Ubuntu 22.04)
```bash
git clone https://github.com/rockchip-linux/rknn-toolkit2

# 安装依赖 (根据 Python 版本选择)
pip3 install -r requirements_cpxx-1.6.0.txt

# 安装 toolkit
pip3 install rknn_toolkit2-x.x.x+xxxxxxxx-cpxx-linux_x86_64.whl
```

#### Conda 环境安装 (推荐)
```bash
conda create -n RKNN-Toolkit2 python=3.9
conda activate RKNN-Toolkit2

pip install -r requirements_cp39-2.3.2.txt
pip install rknn_model_zoo/packages/x86_64/rknn_toolkit2-2.3.2-cp39-cp39-manylinux_2_17_x86_64.whl
```

### 10.3 模型转换流程

#### ONNX 模型获取
支持 PyTorch、TensorFlow 等框架导出。可用 Netron 工具查看模型结构。

#### 人脸检测 (Retinaface)
```bash
git clone https://github.com/bubbliiiing/retinaface-pytorch.git
conda create -n retinaface python=3.6
python export_onnx.py
```

#### 人脸特征提取 (Facenet)
```bash
git clone https://github.com/bubbliiiing/facenet-pytorch.git
# 需移除 RKNPU 不支持的 ReduceL2 算子
python export_onnx.py
```

#### 物体识别 (YOLOv5)
```bash
git clone https://github.com/airockchip/yolov5.git
python export.py --rknpu --weight yolov5s.pt
```

### 10.4 ONNX 转 RKNN

```bash
cd luckfox_pico_rknn_example/scripts/luckfox_onnx_to_rknn/convert

# 通用格式
python convert.py <onnx模型> <训练集> <输出路径> <模型类型>

# 示例
python convert.py ../model/retinaface.onnx ../dataset/retinaface_dataset.txt ../model/retinaface.rknn Retinaface
```

### 10.5 模型部署 C API

#### 初始化
```c
rknn_init(&ctx, mode_path, 0, 0, NULL);

// 获取输入输出通道数
rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM, &io_num, sizeof(io_num));

// 获取输入属性
rknn_query(ctx, RKNN_QUERY_NATIVE_INPUT_ATTR, &input_attrs[i], sizeof(rknn_tensor_attr));
```

#### 内存管理
```c
rknn_tensor_mem* input_mems[i] = rknn_create_mem(ctx, input_attrs[i].size_with_stride);
rknn_set_io_mem(ctx, input_mems[i], &input_attrs[0]);
```

#### 推理执行
```c
memcpy(input_mems[0]->virt_addr, src_image, width * height * channels);
rknn_run(ctx, nullptr);

// 量化逆转
float deqnt = ((float)qnt - (float)zp) * scale;
```

### 10.6 编译与运行示例

#### PC 端编译
```bash
export LUCKFOX_SDK_PATH=<sdk地址>
./build.sh
# 选择编译示例:
# 1. luckfox_pico_retinaface_facenet
# 2. luckfox_pico_retinaface_facenet_spidev
# 3. luckfox_pico_yolov5
```

#### 板端执行
```bash
cd <Demo Dir>
chmod a+x <Demo Target>

# 人脸检测+识别
./luckfox_pico_retinaface_facenet ./model/RetinaFace.rknn ./model/mobilefacenet.rknn ./model/test.jpg

# YOLOv5 物体检测
./luckfox_pico_yolov5 ./model/yolov5.rknn
```

### 10.7 rknn_model_zoo 部署

```bash
# 模型转换
conda activate RKNN-Toolkit2
python3 convert.py ../model/yolov5s.onnx rv1106

# 交叉编译
export GCC_COMPILER=<SDK目录>/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin/arm-rockchip830-linux-uclibcgnueabihf
./build-linux.sh -t rv1106 -a armv7l -d yolov5

# 板端运行
./rknn_yolov5_demo model/yolov5.rknn model/bus.jpg
```

### 10.8 注意事项

- 仅支持 int8 类型输入输出
- 仅支持 4 维输入和输出维度
- 需执行 `RkLunch-stop.sh` 关闭系统 rkipc 程序解除摄像头占用
- 软件模拟器输出为浮点格式，需量化逆转后与硬件对比

---

## 11. RKMPI 多媒体处理

### 11.1 架构概述

Rockchip Media Process Interface (RKMPI) 分层架构:
- 应用层
- RKMPI 层
- 操作系统适配层
- 操作系统层
- 硬件层

### 11.2 VI (视频输入) API

```c
RK_MPI_VI_EnableDev(devID);                          // 启动设备
RK_MPI_VI_SetDevBindPipe(devID, &stBindPipe);        // 绑定管道
RK_MPI_VI_SetChnAttr(PipeID, ChnID, &Chn_attr);     // 设置通道属性
RK_MPI_VI_EnableChn(PipeID, ChnID);                  // 启动通道
RK_MPI_VI_DisableChn(PipeID, ChnID);                 // 关闭通道
```

ISP 初始化:
```c
SAMPLE_COMM_ISP_Init(CamId, hdr_mode, multi_sensor, iq_dir);
SAMPLE_COMM_ISP_Run(CamId);
SAMPLE_COMM_ISP_Stop(CamId);
```

### 11.3 VPSS (视频处理子系统) API

```c
RK_MPI_VPSS_CreateGrp(GrpID, &VpssGrpAttr);                   // 创建组
RK_MPI_VPSS_SetChnAttr(GrpID, ChnID, &VpssChnAttr);           // 设置通道属性
RK_MPI_VPSS_EnableChn(GrpID, ChnID);                           // 启动通道
RK_MPI_VPSS_StartGrp(GrpID);                                   // 启动组
RK_MPI_VPSS_GetChnFrame(GrpID, ChnID, &VpssFrame, -1);        // 获取帧数据
RK_MPI_VPSS_StopGrp(GrpID);                                    // 停止组
RK_MPI_VPSS_DestroyGrp(GrpID);                                 // 销毁组
```

### 11.4 VENC (视频编码) API

```c
RK_MPI_VENC_CreateChn(chnId, &stAttr);                          // 创建通道
RK_MPI_VENC_StartRecvFrame(ChnId, &stRecvParam);               // 开始接收
RK_MPI_VENC_SendFrame(vpssChn, &stVpssFrame, -1);              // 发送帧
RK_MPI_VENC_GetStream(ChnId, &stFrame, -1);                    // 获取编码流
RK_MPI_VENC_StopRecvFrame(ChnId);                               // 停止接收
RK_MPI_VENC_DestroyChn(ChnId);                                  // 销毁通道
```

编码格式:
```c
stAttr.stVencAttr.enType = RK_VIDEO_ID_AVC;           // H.264
stAttr.stRcAttr.enRcMode = VENC_RC_MODE_H264CBR;     // CBR 码率控制
```

### 11.5 内存管理 API

```c
RK_MPI_MB_CreatePool(&PoolCfg);                        // 创建内存池
RK_MPI_MB_GetMB(src_Pool, size, RK_TRUE);             // 分配内存块
RK_MPI_MB_Handle2VirAddr(pMbBlk);                      // 转虚拟地址
RK_MPI_MB_ReleaseMB(src_Blk);                          // 释放内存块
RK_MPI_MB_DestroyPool(src_Pool);                       // 销毁内存池
```

### 11.6 系统绑定 API

```c
RK_MPI_SYS_Init();                                     // 系统初始化
RK_MPI_SYS_Bind(&stSrcChn, &otherChn);                // 绑定模块
RK_MPI_SYS_UnBind(&viChn, &otherChn);                 // 解除绑定
RK_MPI_SYS_Exit();                                     // 系统退出
```

### 11.7 RTSP 推流 API

```c
create_rtsp_demo(port);                                          // 创建 RTSP 实例
rtsp_new_session(g_rtsplive, path);                             // 创建会话
rtsp_set_video(g_rtsp_session, RTSP_CODEC_ID_VIDEO_H264, NULL, 0);  // 设置视频格式
rtsp_sync_video_ts();                                            // 同步时间戳
rtsp_tx_video(session, data, len, PTS);                         // 传输视频数据
rtsp_do_event(g_rtsplive);                                       // 驱动事件
rtsp_del_demo(g_rtsplive);                                       // 删除实例
```

### 11.8 像素格式
- `RK_FMT_YUV420SP`: VI 捕获格式
- `RK_FMT_RGB888`: RKNN 推理输入格式

---

## 12. 音频系统

### 12.1 概述
RV1106 SoC 集成音频处理器，支持外接模拟麦克风 (模数转换)。仅适用于 Buildroot 系统。

### 12.2 音频设备

```bash
# 列出录音设备
arecord -l

# 列出播放设备
aplay -l
```

设备驱动文件:
- `controlC0`: 声卡控制接口
- `pcmC0D0c`: PCM 录音设备
- `pcmC0D0p`: PCM 播放设备

### 12.3 Codec 控制参数

| 参数 | 范围 | 说明 |
|------|------|------|
| ADC MIC Gain | 0-3 | 麦克风输入放大 |
| ADC Digital Volume | 0-255 | 数字增益 |
| DAC LINEOUT Volume | 0-30 | 喇叭输出电平 (1.5dB步进) |
| ADC Mode | 差分/单端 | 输入配置模式 |
| MICBIAS Voltage | - | 麦克风偏置电压 |

### 12.4 录音命令

```bash
# RKMPI 方式
rk_mpi_ai_test --sound_card_name=hw:0,0 --device_rate=16000 --device_ch=2

# ALSA 方式 (16kHz, 双通道, 16bit, 30秒)
arecord -f S16_LE -c 2 -r 16000 -D hw:0 -d 30 test.wav
```

### 12.5 播放命令

```bash
# PCM 文件
rk_mpi_ao_test -i /root/2.pcm --sound_card_name=hw:0,0

# WAV 文件
aplay -Dhw:0 test.wav

# MP3 文件
madplay filename.mp3

# 格式转换
ffmpeg -i input.mp3 -f wav -acodec pcm_s16le -ar 44100 -ac 2 output.wav
```

### 12.6 音量调节

```bash
amixer cset name='DAC LINEOUT Volume' 15   # 范围 0-30
```

### 12.7 麦克风灵敏度增强 (通过 alsamixer)
- ADC ALC gain 调至 75
- ADC MIC Left Gain 调至 100
- MICBIAS 设为 VREFx0_975 (关键)
- ADC Mode 设为单端数字采集

---

## 13. WiFi 与蓝牙

### 13.1 硬件模块
搭载 AIC8800DC 模块，支持:
- WiFi AX (WiFi6) 2.4GHz
- 蓝牙 5.2 / BLE

仅 Ultra W 和 Ultra BW 型号具备无线功能。

### 13.2 WiFi 连接

编辑配置文件 `/etc/wpa_supplicant.conf`，设置 SSID 和密码 (psk)。

```bash
# 启动 WiFi
wpa_supplicant -B -i wlan0 -c /etc/wpa_supplicant.conf

# 获取 IP
udhcpc -i wlan0

# 切换 WiFi 需先终止服务
killall -9 wpa_supplicant
```

#### 开机自启脚本
创建 `/etc/init.d/S99wlan0`，包含 WiFi 启动和停止命令。

### 13.3 WiFi 速率测试

```bash
# 服务器端
iperf3 -s -i 10 -p 5001

# 客户端
iperf3 -c [服务器IP] -p 5001 -f m -i 2 -t 24
```

### 13.4 蓝牙功能

#### A2DP Source (开发板连接蓝牙耳机)
```bash
bluealsa -p a2dp-source &
# bluetoothctl 中:
power on
pairable on
scan on
pair <MAC>
# 播放音频
aplay -D bluealsa:DEV=[地址],PROFILE=a2dp [文件]
```

#### A2DP Sink (手机连接开发板)
```bash
bluetoothd --compat &
bluealsa -p a2dp-sink &
# bluetoothctl 中:
discoverable on
```

---

## 14. 文件传输

### 14.1 ADB 传输
```bash
# 上传文件到开发板
adb push test/ /root

# 从开发板下载文件
adb pull /home/luckfox/1.txt .
```

### 14.2 SCP 传输
```bash
# 上传文件
scp luckfox.txt root@192.168.10.95:/root

# 上传目录
scp -r luckfox root@192.168.10.95:/root

# 下载文件
scp root@192.168.10.95:/root/luckfox.txt .

# 下载目录
scp -r root@192.168.10.95:/root/luckfox .
```

### 14.3 SFTP 传输
使用 electerm 等 SSH/SFTP 客户端，支持 Linux、macOS、Windows。

---

## 15. 自启动与静态 IP

### 15.1 自启动脚本配置

在 `/etc/init.d/` 目录中创建以 `S` 开头的脚本。系统开机后执行 rcS，循环执行 S 开头脚本的 start 分支。

命名规范: `S??*` (?? 为优先级数字，数字越小越先执行)

脚本模板:
```bash
#!/bin/sh
case $1 in
  start)
    # 启动逻辑
    ;;
  stop)
    # 停止逻辑
    ;;
  *)
    exit 1
    ;;
esac
```

### 15.2 静态 IP 配置

#### 网线直连
```bash
ifconfig eth0 192.168.10.200 netmask 255.255.252.0
route add default gw 192.168.11.1
echo "nameserver 114.114.114.114" > /etc/resolv.conf
```

#### 路由器/交换机
需确保静态 IP 地址与路由器网段一致，避免与 DHCP 分配 IP 冲突。脚本应先检测 DHCP 获取状态再配置静态 IP。

---

## 16. Linux 基础与分区信息

### 16.1 存储分区配置

#### eMMC 分区
```
32K(env), 512K@32K(idblock), 256K(uboot), 32M(boot), 512M(oem), 256M(userdata), 6G(rootfs)
```

#### SPI NAND 分区
```
256K(env), 256K@256K(idblock), 512K(uboot), 4M(boot), 30M(oem), 10M(userdata), 80M(rootfs)
```

**关键规则:**
- 分区用逗号分隔
- 单位支持 K/M/G/T/P/E
- idblock 分区偏移固定不可修改

### 16.2 文件系统类型

| 类型 | 特点 |
|------|------|
| EXT2 | 无日志 |
| EXT3 | 带日志功能 |
| EXT4 | 支持 16TB 文件 |
| XFS | 64 位日志系统，处理大文件和高并发 |
| JFFS2 | 闪存专用，擦写平衡与掉电保护 |
| UBIFS | 闪存专用，擦写平衡与掉电保护 |

### 16.3 常用命令

```bash
# 查看挂载信息
df -Th

# 文件权限
chmod 755 file   # 拥有者完全权限，他人可读可执行
chmod 644 file   # 拥有者读写，他人只读

# 目录操作
ls -lh           # 详细列表，易读格式
mkdir -p path    # 创建多级目录
cp -r src dst    # 递归复制目录
```

---

## 附录: 资源链接

| 资源 | 地址 |
|------|------|
| 官方商店 | https://www.luckfox.com |
| 官方论坛 | https://forums.luckfox.com |
| GitHub | https://github.com/LuckfoxTECH |
| SDK (Gitee) | https://gitee.com/LuckfoxTECH/luckfox-pico.git |
| SDK (GitHub) | https://github.com/LuckfoxTECH/luckfox-pico.git |
| AI 助手 | https://ai.luckfox.com |
| Wiki 首页 | https://wiki.luckfox.com/zh/Luckfox-Pico-Ultra |
