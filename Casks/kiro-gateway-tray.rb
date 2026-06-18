cask "kiro-gateway-tray" do
  version "0.1.13"

  on_arm do
    sha256 "49f085d2eff8981c000249831d18bd499c89282c86f4383e5c37152112cfa6ac"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "c43544e5d2fafb46c5e56d32b8c84db9f24bc361f4763bb85e3bfd97f8beeb64"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-amd64.dmg"
  end

  name "Kiro Gateway Tray"
  desc "Cross-platform tray app for kiro-gateway"
  homepage "https://github.com/zhujunsan/kiro-gateway-deploy"

  app "KiroGatewayTray.app"

  caveats <<~EOS
    本 App 暂未签名 / 公证。首次打开前请执行一次去隔离命令，否则会被 Gatekeeper 拦：
      xattr -dr com.apple.quarantine "#{appdir}/KiroGatewayTray.app"
    或在「系统设置 → 隐私与安全性」里点「仍要打开」。
  EOS
end
