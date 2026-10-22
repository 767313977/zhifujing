/**
 * 复制文本到剪贴板。返回是否成功（**不抛异常**，失败由调用方给提示文案）。
 *
 * 不用 `navigator.clipboard`：它只在**安全上下文**（HTTPS / localhost）可用，
 * 而站点在云端是 `http://IP:8080`、本机用手机走局域网也是 `http://10.x.x.x`，
 * 那两种环境调用会直接抛错。textarea + `execCommand` 这套老办法都能用。
 *
 * 调用方自己管「已复制」的状态与定时器（两个页面的时长和文案都不同）。
 */
export function copyText(value: string): boolean {
  const box = document.createElement('textarea')
  box.value = value
  // 放到视口外，避免复制瞬间页面跳动
  box.style.position = 'fixed'
  box.style.top = '-1000px'
  document.body.appendChild(box)
  box.select()
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  document.body.removeChild(box)
  return ok
}
