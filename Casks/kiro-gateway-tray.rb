cask "kiro-gateway-tray" do
  version "0.1.20"

  on_arm do
    sha256 "e60e95a8f439c7adfb70f3b02654ebcc937cb3894d99752073003919920069b4"
    url "https://github.com/zhujunsan/kiro-gateway-deploy/releases/download/v#{version}/KiroGatewayTray-#{version}-macos-arm64.dmg"
  end
  on_intel do
    sha256 "64c7d7114da8449c8c5bd110b8274dbf485b9ae9a691b17ed16f3aaf0a1363e5"
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
