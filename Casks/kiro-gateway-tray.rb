cask "kiro-gateway-tray" do
  version "0.4.27"

  on_arm do
    sha256 "ddbe6ab93fcf829e96a82df9b997f8c6e8a23abf92bb18971c35fb33b360894a"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "3dac7adec9006f6383980cf7eeaf86dc2c9e0e7605d7530242c92c32621911f0"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-amd64.dmg"
  end

  name "Kiro Gateway Tray"
  desc "Cross-platform tray app for kiro-gateway"
  homepage "https://github.com/zhujunsan/kiro-gateway-deploy"

  app "KiroGatewayTray.app"

  postflight do
    system_command "/usr/bin/xattr",
                   args: ["-dr", "com.apple.quarantine", "#{appdir}/KiroGatewayTray.app"],
                   sudo: false
  end
end
