package vade.spike.baseline

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * Size control group: the same eight sections as the Miuix spike, drawn with
 * Material 3 primitives only. Nothing here is meant to look like Miuix; the
 * point is the byte delta between `material3` and `material3 + miuix-ui`.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun BaselineApp() {
    var text by remember { mutableStateOf("本地会话") }
    var on by remember { mutableStateOf(true) }
    var tab by remember { mutableStateOf(0) }
    var showDialog by remember { mutableStateOf(false) }
    val labels = listOf("聊天", "会话", "设置")

    MaterialTheme {
        Scaffold(
            topBar = { TopAppBar(title = { Text("Vade · Material 3 baseline") }) },
            bottomBar = {
                NavigationBar {
                    labels.forEachIndexed { index, label ->
                        NavigationBarItem(
                            selected = tab == index,
                            onClick = { tab = index },
                            icon = { Text("●") },
                            label = { Text(label) },
                        )
                    }
                }
            },
        ) { padding ->
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(padding)
                    .verticalScroll(rememberScrollState())
                    .padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp),
            ) {
                // 1 Button
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = {}) { Text("主操作") }
                    TextButton(onClick = {}) { Text("次操作") }
                }
                // 2 TextField
                OutlinedTextField(
                    value = text,
                    onValueChange = { text = it },
                    label = { Text("会话名称") },
                    modifier = Modifier.fillMaxWidth(),
                )
                // 3 Card + 4 ListItem
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp)) {
                        Text("卡片标题", style = MaterialTheme.typography.titleMedium)
                        Text("用于验证阴影/描边与内边距。")
                        Row(
                            modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
                            horizontalArrangement = Arrangement.SpaceBetween,
                        ) {
                            Column {
                                Text("本地会话")
                                Text("seq 1024 · 已同步", style = MaterialTheme.typography.bodySmall)
                            }
                            Text("›")
                        }
                        Row(
                            modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
                            horizontalArrangement = Arrangement.SpaceBetween,
                        ) {
                            Column {
                                Text("云端设备")
                                Text("未连接", style = MaterialTheme.typography.bodySmall)
                            }
                            Switch(checked = on, onCheckedChange = { on = it })
                        }
                    }
                }
                // 5 Switch
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Switch(checked = on, onCheckedChange = { on = it })
                    Text(if (on) "深色模式：开" else "深色模式：关")
                }
                // 6 BottomBar is the Scaffold slot above.
                // 7 Dialog
                Button(onClick = { showDialog = true }) { Text("打开确认弹窗") }
                if (showDialog) {
                    AlertDialog(
                        onDismissRequest = { showDialog = false },
                        title = { Text("允许执行工具？") },
                        text = { Text("fs.write · 请求写入 3 个文件") },
                        confirmButton = { TextButton(onClick = { showDialog = false }) { Text("允许") } },
                        dismissButton = { TextButton(onClick = { showDialog = false }) { Text("拒绝") } },
                    )
                }
                // 8 TopAppBar is the Scaffold slot above.
                Text("选中项：${labels[tab]}")
            }
        }
    }
}
