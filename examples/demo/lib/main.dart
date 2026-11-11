import 'package:flutter/material.dart';

void main() {
  runApp(const YepTodoApp());
}

class YepTodoApp extends StatelessWidget {
  const YepTodoApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Yep Todo',
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.indigo),
      ),
      home: const TodoPage(),
    );
  }
}

class Task {
  Task(this.title, {this.done = false});

  final String title;
  bool done;
}

class TodoPage extends StatefulWidget {
  const TodoPage({super.key});

  @override
  State<TodoPage> createState() => _TodoPageState();
}

class _TodoPageState extends State<TodoPage> {
  final List<Task> _tasks = [
    Task('Buy milk'),
    Task('Walk the dog'),
    Task('Ship it'),
  ];

  void _addTask(String title) {
    setState(() => _tasks.add(Task(title)));
  }

  void _openAddSheet() {
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (_) => _AddTaskSheet(onSave: _addTask),
    );
  }

  @override
  Widget build(BuildContext context) {
    final doneCount = _tasks.where((task) => task.done).length;

    return Scaffold(
      appBar: AppBar(title: const Text('Yep Todo')),
      body: ListView(
        children: [
          for (final task in _tasks)
            ListTile(
              onTap: () => setState(() => task.done = !task.done),
              leading: Checkbox(
                value: task.done,
                onChanged: (value) => setState(() => task.done = value ?? false),
              ),
              title: Text(
                task.title,
                style: task.done
                    ? const TextStyle(decoration: TextDecoration.lineThrough)
                    : null,
              ),
            ),
          if (_tasks.isNotEmpty)
            Padding(
              padding: const EdgeInsets.all(16),
              child: Text('$doneCount of ${_tasks.length} done'),
            ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _openAddSheet,
        icon: const Icon(Icons.add),
        label: const Text('Add task'),
      ),
    );
  }
}

class _AddTaskSheet extends StatefulWidget {
  const _AddTaskSheet({required this.onSave});

  final ValueChanged<String> onSave;

  @override
  State<_AddTaskSheet> createState() => _AddTaskSheetState();
}

class _AddTaskSheetState extends State<_AddTaskSheet> {
  final TextEditingController _controller = TextEditingController();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _save() {
    final title = _controller.text.trim();
    if (title.isEmpty) {
      return;
    }
    widget.onSave(title);
    Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.fromLTRB(
        24,
        24,
        24,
        24 + MediaQuery.viewInsetsOf(context).bottom,
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('New task', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 16),
          TextField(
            controller: _controller,
            autofocus: true,
            decoration: const InputDecoration(labelText: 'Task title'),
            onSubmitted: (_) => _save(),
          ),
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _save,
            child: const Text('Save'),
          ),
        ],
      ),
    );
  }
}
