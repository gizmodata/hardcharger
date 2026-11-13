# bridge.cc exports Java_com_gizmodata_hardcharger_NativeBridge_* symbols.
# Preserve only this JNI boundary; the rest of the app remains optimizable.
-keep class com.gizmodata.hardcharger.NativeBridge {
    native <methods>;
}
