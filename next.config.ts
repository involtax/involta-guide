import type { NextConfig } from "next";
const config: NextConfig = {
  serverExternalPackages: ["node:sqlite"],
  experimental: {
    useTypeScriptCli: false,
    serverActions: { bodySizeLimit: "1mb" },
  },
};
export default config;
