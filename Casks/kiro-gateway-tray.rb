cask "kiro-gateway-tray" do
  version "0.4.3"

  on_arm do
    sha256 "e96a4db9d7eb270da33130934f8b9fe34a059b44db2a8702718fc60015719b74"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "c79b0c7d3b532c6f0e8447f8f444f725d444a15259b655323cd94d7c617615f8"
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
