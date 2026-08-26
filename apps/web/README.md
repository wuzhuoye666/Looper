# Looper Web

独立的 React/Vite/TypeScript 前端。开发模式默认连接 `http://127.0.0.1:8000/api/v1`；生产构建默认使用当前网站的 `/api/v1`，由 Nginx 转发到后端。

```bash
npm install
npm run dev
npm test
npm run build
```

`.env.production` 为生产构建设置 `VITE_API_BASE_URL=/api/v1`，覆盖开发者 `.env.local` 中的本机地址。需要独立 API 时，可通过进程环境变量或 `.env.production.local` 覆盖。公网部署不要将它设置为 `localhost` 或 `127.0.0.1`，否则浏览器会访问访客自己的电脑。
