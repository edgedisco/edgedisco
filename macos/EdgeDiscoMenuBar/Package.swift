// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "EdgeDiscoMenuBar",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "EdgeDiscoMenuBar", targets: ["EdgeDiscoMenuBar"]),
    ],
    dependencies: [
        .package(path: "../EdgeDiscoIPC"),
    ],
    targets: [
        .executableTarget(
            name: "EdgeDiscoMenuBar",
            dependencies: [
                .product(name: "EdgeDiscoIPC", package: "EdgeDiscoIPC"),
            ]
        ),
        .testTarget(
            name: "EdgeDiscoMenuBarTests",
            dependencies: [
                "EdgeDiscoMenuBar",
                .product(name: "EdgeDiscoIPC", package: "EdgeDiscoIPC"),
            ]
        ),
    ]
)
