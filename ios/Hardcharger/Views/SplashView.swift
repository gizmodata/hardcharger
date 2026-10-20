import SwiftUI

/// The splash: the charging bull with the Hardcharger™ lettering in the middle, exactly where
/// the launch screen (Info.plist UILaunchScreen) drew it, and the GizmoData mark at the
/// bottom, while the server starts.
struct SplashView: View {
    @Environment(ServerController.self) private var server
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var appeared = false

    var body: some View {
        ZStack {
            Color("LaunchBackground").ignoresSafeArea()

            Image("Mascot")
                .accessibilityLabel("Hardcharger™")
                .scaleEffect(appeared && !reduceMotion ? 1.02 : 1)
                .animation(reduceMotion ? nil : .easeInOut(duration: 1.6).repeatForever(autoreverses: true),
                           value: appeared)
                .ignoresSafeArea()

            VStack(spacing: 18) {
                Spacer()
                status
                    .opacity(appeared ? 1 : 0)
                ByGizmoData()
                    .opacity(appeared ? 1 : 0)
                    .padding(.bottom, 8)
            }
            .padding(.bottom, 24)
            .animation(.easeOut(duration: 0.6).delay(0.2), value: appeared)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("splash")
        .onAppear { appeared = true }
    }

    @ViewBuilder private var status: some View {
        switch server.phase {
        case .starting:
            Label {
                Text("Starting the server…")
            } icon: {
                ProgressView().controlSize(.small)
            }
            .font(.subheadline.weight(.medium))
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .hcGlass(cornerRadius: 999)
        case .running:
            Label("Ready", systemImage: "bolt.fill")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.tint)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .hcGlass(cornerRadius: 999)
        default:
            EmptyView()
        }
    }
}

/// The GizmoData mark with its name: the splash's and About's footer.
struct ByGizmoData: View {
    var body: some View {
        HStack(spacing: 10) {
            Image("GizmoDataMark")
                .resizable()
                .scaledToFit()
                .frame(height: 30)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 0) {
                Text("by")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text(verbatim: "GizmoData™")
                    .font(.headline)
                    .foregroundStyle(Color("BrandNavy"))
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("by GizmoData™")
    }
}
