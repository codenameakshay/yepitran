import 'package:flutter_test/flutter_test.dart';

import 'package:yepitran_demo/main.dart';

void main() {
  testWidgets('home shows title and add button', (tester) async {
    await tester.pumpWidget(const YepTodoApp());

    expect(find.text('Yep Todo'), findsOneWidget);
    expect(find.text('Add task'), findsOneWidget);
  });
}
