struct SettingsNumericInput {
    let intervalSeconds: UInt64
    let batchSize: Int

    init?(interval: String, batchSize: String) {
        guard let seconds = UInt64(interval), seconds > 0,
              let batch = Int(batchSize), batch > 0 else { return nil }
        self.intervalSeconds = seconds
        self.batchSize = batch
    }
}
