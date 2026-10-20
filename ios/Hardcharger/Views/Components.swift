import CoreImage.CIFilterBuiltins
import SwiftUI

extension View {
    /// Liquid Glass on iOS 26 and later; a material elsewhere.
    @ViewBuilder
    func hcGlass(cornerRadius: CGFloat = 16, interactive: Bool = false) -> some View {
        if #available(iOS 26.0, *) {
            glassEffect(interactive ? .regular.interactive() : .regular,
                        in: .rect(cornerRadius: cornerRadius, style: .continuous))
        } else {
            background(.regularMaterial, in: .rect(cornerRadius: cornerRadius, style: .continuous))
        }
    }

    /// A prominent glass button on iOS 26 and later, a bordered prominent one elsewhere.
    @ViewBuilder
    func hcProminentButton() -> some View {
        if #available(iOS 26.0, *) {
            buttonStyle(.glassProminent)
        } else {
            buttonStyle(.borderedProminent)
        }
    }

    @ViewBuilder
    func hcButton() -> some View {
        if #available(iOS 26.0, *) {
            buttonStyle(.glass)
        } else {
            buttonStyle(.bordered)
        }
    }
}

/// The server's state as a coloured capsule: "Running", "Stopped", …
struct StatusBadge: View {
    let phase: ServerController.Phase

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
                .overlay {
                    if phase == .running {
                        Circle().stroke(color.opacity(0.5), lineWidth: 4).blur(radius: 2)
                    }
                }
            Text(title)
                .font(.subheadline.weight(.semibold))
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(color.opacity(0.14), in: Capsule())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text("Server status: \(Text(title))"))
        .accessibilityIdentifier("server.status")
        .accessibilityValue(Text(title))
    }

    private var title: LocalizedStringResource {
        switch phase {
        case .stopped: "Stopped"
        case .starting: "Starting"
        case .running: "Running"
        case .stopping: "Stopping"
        case .failed: "Failed"
        }
    }

    private var color: Color {
        switch phase {
        case .running: .green
        case .starting, .stopping: .orange
        case .stopped: .secondary
        case .failed: .red
        }
    }
}

/// A QR code of `text`, drawn crisp at any size.
struct QRCodeView: View {
    let text: String

    var body: some View {
        if let image = Self.image(for: text) {
            Image(uiImage: image)
                .interpolation(.none)
                .resizable()
                .scaledToFit()
                .padding(10)
                .background(.white, in: .rect(cornerRadius: 12))
                .accessibilityLabel(Text("QR code for \(text)"))
        }
    }

    static func image(for text: String) -> UIImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(text.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage,
              let cg = CIContext().createCGImage(output, from: output.extent)
        else { return nil }
        return UIImage(cgImage: cg)
    }
}

/// A value with a copy button, in a list row.
struct CopyableRow: View {
    let title: LocalizedStringKey
    let value: String
    var monospaced = false
    var secret = false
    @State private var revealed = false
    @State private var copied = false

    var body: some View {
        // Long values (addresses, passwords) go under their title, with the whole row's width
        // to wrap in at large text sizes.
        if monospaced {
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    valueText
                        .frame(maxWidth: .infinity, alignment: .leading)
                    buttons
                }
            }
        } else {
            LabeledContent {
                HStack(spacing: 8) {
                    valueText
                    buttons
                }
            } label: {
                Text(title)
            }
        }
    }

    private var valueText: some View {
        Text(secret && !revealed ? String(repeating: "•", count: 12) : value)
            .font(.system(.callout, design: monospaced ? .monospaced : .default))
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
            .accessibilityLabel(Text(title))
            .accessibilityValue(secret && !revealed ? Text("Hidden") : Text(verbatim: value))
    }

    @ViewBuilder private var buttons: some View {
        if secret {
            Button {
                revealed.toggle()
            } label: {
                Image(systemName: revealed ? "eye.slash" : "eye")
            }
            .buttonStyle(.borderless)
            .accessibilityLabel(revealed ? Text("Hide") : Text("Show"))
        }
        Button {
            UIPasteboard.general.string = value
            copied = true
            Task {
                try? await Task.sleep(for: .seconds(1.5))
                copied = false
            }
        } label: {
            Image(systemName: copied ? "checkmark" : "doc.on.doc")
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(.borderless)
        .accessibilityLabel(Text("Copy"))
    }
}

/// Byte counts as people read them.
func formattedBytes(_ bytes: Int64) -> String {
    ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
}

/// The app's own memory use (resident), as iOS counts it against the app.
func appMemoryFootprint() -> Int64 {
    var info = task_vm_info_data_t()
    var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
    let result = withUnsafeMutablePointer(to: &info) {
        $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
            task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
        }
    }
    return result == KERN_SUCCESS ? Int64(info.phys_footprint) : 0
}
