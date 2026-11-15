# 修复前浏览器证据（浏览器原文摘要）

- 环境：`http://127.0.0.1:8021`，修复前构建 `9838a06`（`assets/index-icdFEUK--r2.js`）
- 账号：隔离 Staging 测试账号（密码不落盘）
- 工具：TRAE 浏览器代理；未修改应用代码

## 第 1 轮：直接复现

```text
location.pathname = "/login"
#root.childElementCount = 0
#root.innerHTML.length = 0
document.body.innerText.length = 0
可见标题 = 0；可见 nav/header 链接 = 0
网络：POST /api/auth/login 200 → GET /api/auth/me 200 → GET /api/watchlist 200
失败请求：全程仅未登录时的 GET /api/auth/me 401（预期）
控制台：none；error/unhandledrejection：无
强制刷新 "/"：pathname=/、#root=1、innerHTML=22037、bodyLen=933
```

## 第 2 轮：加装 error / unhandledrejection / console.error 监听

```text
结果与第 1 轮一致（/login、root=0、bodyLen=0）
捕获数组为空：无 error、无 unhandledrejection、无 console.error
登录后无 4xx/5xx、无后续文档导航
```

## 第 3 轮：插桩 history.pushState / replaceState

```text
history 调用（3 条，含调用栈）：
1) replaceState -> "/"   caller: Login.navigate(from,{replace:true})
2) replaceState -> "/"   caller: Login <Navigate to={from} replace />
3) pushState   -> "/login"
   caller: StockResearchProvider 的 setSearchParamsRef.current（Object.current）
最终 history.state = {idx:"1", key:"zf796mh0", masked:"undefined", usr:"null"}
location.href = http://127.0.0.1:8021/login
#root.outerHTML = <div id="root"></div>
无 popstate / hashchange
fupan.auth.event = {"type":"login","at":...}
```

## 结论

第 3 条 `pushState("/login")` 在两次 `replaceState("/")` 之后把路由打回登录页，
并清空 `state.usr`；`Login` 随后只渲染 `<Navigate>`（DOM=null），effect 依赖未变
不会重跑 → 空树。与单元测试（修复前 3/3 失败）一致。
