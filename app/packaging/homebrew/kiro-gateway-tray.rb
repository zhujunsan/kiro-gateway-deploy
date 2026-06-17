cask "kiro-gateway-tray" do
  version "0.1.0"
  sha256 :no_check

  url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/app-v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  name "Kiro Gateway Tray"
  desc "Cross-platform tray app for kiro-gateway"
  homepage "https://github.com/zhujunsan/kiro-gateway-deploy"

  app "KiroGatewayTray.app"
end
