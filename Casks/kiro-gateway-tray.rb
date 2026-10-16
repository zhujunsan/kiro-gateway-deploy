cask "kiro-gateway-tray" do
  version "0.5.4"

  on_arm do
    sha256 "b52c1cb1f01b5ed63e9dc5374cff79e60fddce4ea935675f0e46d0c7132d3ff7"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "f58f5361d759ffeb0e939d80219e0da8c632feefa436075117c8a6a1b927d39d"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-amd64.dmg"
  end

  name "Kiro Gateway Tray"
  desc "Cross-platform tray app for kiro-gateway"
  homepage "https://github.com/zhujunsan/kiro-gateway-deploy"

  app "KiroGatewayTray.app"

  postflight_steps do
    run "/usr/bin/xattr",
        args: ["-dr", "com.apple.quarantine", "{{appdir}}/KiroGatewayTray.app"]
  end
end
