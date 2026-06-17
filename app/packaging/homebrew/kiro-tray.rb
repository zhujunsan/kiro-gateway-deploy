cask "kiro-tray" do
  version "0.1.0"
  sha256 :no_check

  url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/app-v#{version}/KiroTray-#{version}-macos-arm64.zip"
  name "Kiro Tray"
  desc "Cross-platform tray app for kiro-gateway"
  homepage "https://github.com/zhujunsan/kiro-gateway-deploy"

  app "KiroTray.app"
end
