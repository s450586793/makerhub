export const VIDEO_DURATION_SECONDS = 45;

export const storyboardSegments = [
  {
    id: "model-library",
    start: 0,
    duration: 4,
    title: "私有模型库",
    visual: "模型库封面网格快速推进，突出模型卡片细节。",
    voiceover: "把 MakerWorld 模型，整理成你自己的私有模型库。",
  },
  {
    id: "dashboard",
    start: 4,
    duration: 4,
    title: "一个工作台看全局",
    visual: "首页状态卡片扫过归档、订阅、本地导入、任务和源站状态。",
    voiceover: "归档、订阅、导入、任务和源站状态，都集中在一个工作台。",
  },
  {
    id: "online-sync",
    start: 8,
    duration: 6,
    title: "登录后自动同步",
    visual: "线上账号区域，随后切到同步后的订阅来源卡片。",
    voiceover: "登录线上账号，关注作者、收藏夹和合集会自动进入订阅库。",
  },
  {
    id: "subscriptions",
    start: 14,
    duration: 5,
    title: "持续发现新模型",
    visual: "订阅库/来源库卡片，以及来源详情中的模型聚合。",
    voiceover: "MakerHub 会按订阅来源持续检查，把新模型加入归档流程。",
  },
  {
    id: "remote-refresh",
    start: 19,
    duration: 6,
    title: "保持源端信息新鲜",
    visual: "源端刷新页面，展示运行进度和结果摘要。",
    voiceover: "源端刷新会更新评论、附件、打印配置和模型状态。",
  },
  {
    id: "source-deleted",
    start: 25,
    duration: 5,
    title: "识别源端删除",
    visual: "模型卡片或详情页上的源端删除/远端状态标记。",
    voiceover: "如果源站模型已经消失，本地资料库也能清楚标记。",
  },
  {
    id: "local-upload",
    start: 30,
    duration: 5,
    title: "本地模型也能整理",
    visual: "本地上传入口和本地整理进度。",
    voiceover: "3MF、STL、STEP、OBJ 和压缩包，可以从网页、手机或本地文件夹导入。",
  },
  {
    id: "sharing",
    start: 35,
    duration: 5,
    title: "模型安全分享",
    visual: "分享弹窗和接收分享入口，敏感码隐藏或避开。",
    voiceover: "生成分享码，把模型发送给另一台 MakerHub，并自动检查重复。",
  },
  {
    id: "verification",
    start: 40,
    duration: 5,
    title: "验证后继续归档",
    visual: "首页验证异常提示和重试入口。",
    voiceover: "遇到 MakerWorld 验证，去源站完成验证，回到 MakerHub 重试即可。",
  },
];

export function storyboardById(id) {
  return storyboardSegments.find((segment) => segment.id === id) || null;
}
