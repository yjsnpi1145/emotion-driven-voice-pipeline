# 模型与引擎许可证

## 分发边界

**模型权重不包含在本仓库，也不包含在本项目构建的 wheel 中。** 安装脚本只会在用户
明确接受相应模型许可证后，把固定版本资产下载到 Git 忽略的 `external/` 或 `runtime/`
目录。本项目的 Apache-2.0 许可证不会覆盖、替代或重新许可这些资产。

用户导入的 GPT-SoVITS `.ckpt`/`.pth` 模型、训练数据、参考音色和生成音频同样不属于
本项目分发内容。发布这些文件前，使用者需要自行确认声音授权、训练数据版权和底模条款。

## IndexTTS2

- 源码仓库：<https://github.com/index-tts/index-tts>
- 固定提交：`90ca4d608209584bad3a5bd5becc0b80c146e60f`
- IndexTTS-2 模型固定 revision：`740dcaff396282ffb241903d150ac011cd4b1ede`
- 固定版本许可证：<https://github.com/index-tts/index-tts/blob/90ca4d608209584bad3a5bd5becc0b80c146e60f/LICENSE>
- 许可证名称：bilibili Model Use License Agreement

该许可证包含独立的使用、规模、模型改进、下游分发和高风险用途条款。IndexTTS2 不是
本项目 Apache-2.0 许可证的一部分，也不能标记为 MIT。下载得到的 checkpoint 目录必须
保留上游提供的 `LICENSE.txt`、`LICENSE_ZH.txt` 和版权声明。

## GPT-SoVITS

- 源码仓库：<https://github.com/RVC-Boss/GPT-SoVITS>
- 固定提交：`d523079fc05d9a8028d6085bffe4a2757c32abb6`
- 源码许可证：<https://github.com/RVC-Boss/GPT-SoVITS/blob/d523079fc05d9a8028d6085bffe4a2757c32abb6/LICENSE>（MIT）
- 预训练资产仓库：`XXXXRT/GPT-SoVITS-Pretrained`
- 预训练资产固定 revision：`4fae8ec36d3d0373864e580b5d8acfba8da29630`

GPT-SoVITS 源码许可证不自动证明任意第三方训练权重、数据集或声音素材可以再分发。用户
训练模型仍由其数据来源、声音授权和所用底模条款共同约束。

## 质量检查与辅助模型

真实模式还会按 `config/engines.lock.yaml` 和 `config/quality-model.lock.yaml` 下载固定
revision 的 faster-whisper、w2v-BERT、MaskGCT、CAMPPlus、BigVGAN 等资产。每个模型
继续适用其上游模型卡和许可证；锁文件只保证来源、版本与哈希，不改变许可条款。

