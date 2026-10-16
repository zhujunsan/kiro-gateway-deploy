cask "kiro-gateway-tray" do
  version "0.5.3"

  on_arm do
    sha256 "de16961b5aaeff039ed1aa646188e8d7435bc4cfe1f685ee252a834e9309e3a1"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "24a43d013837f99f5ef9d6f57eeca998086c28b51c43d18a50cc7483c1affdf3"
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
