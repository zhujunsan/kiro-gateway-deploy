cask "kiro-gateway-tray" do
  version "0.1.14"

  on_arm do
    sha256 "2be29173d501f859e3dfc0f869e35676d75406a82b3223a57461dc1be715f367"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "4ee0a054e9907d9ab6344b6282b3f064e15eb9904d8f55ec038fd84cc51487f8"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-amd64.dmg"
  end

  name "Kiro Gateway Tray"
  desc "Cross-platform tray app for kiro-gateway"
  homepage "https://github.com/zhujunsan/kiro-gateway-deploy"

  app "KiroGatewayTray.app"

  postflight do
    ohai "本 App 暂未签名 / 公证。首次打开前请执行一次去隔离命令，否则会被 Gatekeeper 拦："
    puts %Q{    xattr -dr com.apple.quarantine "#{appdir}/KiroGatewayTray.app"}
    puts "或在「系统设置 → 隐私与安全性」里点「仍要打开」。"
  end
end
