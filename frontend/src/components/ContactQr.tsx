/**
 * 站长的微信二维码（可点的图）。
 *
 * 只包「拿到图」这一件事，抽出来是因为**两个不变量两处都不能少**：
 * 1. **必须自己给白底**（`bg-white`）—— 暗色背景上直接放二维码会扫不出来；
 * 2. 点开看原图 —— 内联尺寸在手机上勉强能扫，放大之后才稳。
 *
 * 文字说明各页自己写（注册页说「给你邀请码」，账号页说「报错/提需求」），所以这里
 * 不接 children。⚠️ 用它的地方**别再套一层 `<a>`**（会变成嵌套 anchor，HTML 不合法）。
 */
const QR_SRC = '/wechat-qr.png'

export default function ContactQr({ size = 104 }: { size?: number }) {
  return (
    <a
      href={QR_SRC}
      target="_blank"
      rel="noreferrer"
      title="点开看原图（放大更好扫）"
      className="shrink-0"
    >
      <img
        src={QR_SRC}
        alt="站长微信二维码"
        width={size}
        height={size}
        className="block bg-white"
      />
    </a>
  )
}
