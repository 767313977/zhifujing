/**
 * 页脚：只剩 ICP 备案号。
 *
 * 管局的要求是「悬挂 ICP 备案号并链接至工信部备案官网首页，否则将被管局责令更改」——
 * 所以这不只是装饰，**别顺手删掉**。
 * （「公安联网备案」是另一套系统的事，不要求悬挂，只要求 30 日内去 beian.mps.gov.cn 提交。）
 *
 * 单独抽成组件（而不是写在 Layout 里）是因为**登录页也要渲染它**：登录页不套 Layout，
 * 而用户第一眼看到的正是那一页。
 *
 * 账号区（用户名 / 改密码 / 退出）2026-09-28 挪去了顶栏右上角，见 `UserMenu`；
 * 样式刻意压到最弱一档（12px + `text-fg-dim`）：它必须存在，但不该和正文抢视线。
 */
export default function Footer() {
  return (
    <footer className="mx-auto max-w-shell px-3 pt-2 pb-6 md:px-5">
      <a
        href="https://beian.miit.gov.cn/"
        target="_blank"
        rel="noreferrer"
        className="num text-[12px] text-fg-dim transition-colors hover:text-fg-muted"
      >
        陕ICP备2026027279号
      </a>
    </footer>
  )
}
