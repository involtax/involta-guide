import type { NextConfig } from "next";
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "/guide";
if (basePath && !/^\/[a-zA-Z0-9/_-]+$/.test(basePath))
  throw new Error(
    "NEXT_PUBLIC_BASE_PATH должен быть путём вроде /guide без завершающего слэша",
  );
if (basePath.endsWith("/"))
  throw new Error("Уберите завершающий слэш NEXT_PUBLIC_BASE_PATH");
const config: NextConfig = {
  basePath,
  serverExternalPackages: ["pg"],
  experimental: {
    useTypeScriptCli: false,
    serverActions: { bodySizeLimit: "1mb" },
  },
};
export default config;
