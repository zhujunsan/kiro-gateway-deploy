cask "kiro-gateway-tray" do
  version "0.1.16"

  on_arm do
    sha256 "90d6545b0f3e0f15bcec0ecc9e233301c56f1acacf37a4dc5ff881a79c39372f"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "762d3e101cc505ac0c391b74e5010a24b35682f4e6677a1dcf7057de85ddc0a6"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-amd64.dmg"
  end

  name "Kiro Gateway Tray"
  desc "Cross-platform tray app for kiro-gateway"
  homepage "https://github.com/zhujunsan/kiro-gateway-deploy"

  app "KiroGatewayTray.app"

  postflight do
    ohai "本 App 暂未签名 / 公证。首次打开前请执行一次去隔离命令，否则会被 Gatekeeper 拦："
    puts %Q{    xattr -dr com.apple.quarantine "#{appdir}/KiroGatewayTray.app"}
    puts %Q{    或在「系统设置 → 隐私与安全性」里点「仍要打开」。}
  end
end
