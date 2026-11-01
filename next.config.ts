import type { NextConfig } from "next";
const config: NextConfig = {
  serverExternalPackages: ["pg"],
  experimental: {
    useTypeScriptCli: false,
    serverActions: { bodySizeLimit: "1mb" },
  },
};
export default config;
