// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "EdgeDiscoIPC",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "EdgeDiscoIPC", targets: ["EdgeDiscoIPC"]),
    ],
    targets: [
        .target(name: "EdgeDiscoIPC"),
        .testTarget(name: "EdgeDiscoIPCTests", dependencies: ["EdgeDiscoIPC"]),
    ]
)
