cask "kiro-gateway-tray" do
  version "0.1.23"

  on_arm do
    sha256 "d198e6bc49a326f7969315d09b8a0e95f1e53b99c2286afd6aa6473206b8ec97"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "e47594b9ed57aaa2c0e4fe075b8b9f431947b60b5a029877889305561d9877e2"
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
