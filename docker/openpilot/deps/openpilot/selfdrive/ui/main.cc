#include <cstdlib>
#include <cstring>
#include <sys/resource.h>
#include <stdio.h>
#include <fstream>
#include <chrono>
#include <thread>

#include <QApplication>
#include <QTranslator>

#include "system/hardware/hw.h"
#include "selfdrive/ui/qt/qt_window.h"
#include "selfdrive/ui/qt/util.h"
#include "selfdrive/ui/qt/window.h"

int main(int argc, char *argv[]) {
  const char* should_debug_ui = std::getenv("DEBUG_UI");

  if (should_debug_ui != NULL && strcmp(should_debug_ui, "1") == 0)
  {
    volatile int waiting = 1;
    while (waiting) {}
  }

  std::ofstream outfile;
  outfile.open("/openpilot/ui_exc.txt", std::ios_base::app);

  for (int i = 0; i < argc; i++)
  {
    outfile << "[" << argv[i] << "] ";
  }
  outfile << '\n';

  setpriority(PRIO_PROCESS, 0, -20);

  qInstallMessageHandler(swagLogMessageHandler);
  initApp(argc, argv);

  QTranslator translator;
  QString translation_file = QString::fromStdString(Params().get("LanguageSetting"));
  if (!translator.load(translation_file, "translations") && translation_file.length()) {
    qCritical() << "Failed to load translation file:" << translation_file;
  }

  QApplication a(argc, argv);
  a.installTranslator(&translator);

  MainWindow w;
  setMainWindow(&w);
  a.installEventFilter(&w);

  try
  {
    // outfile << "sleeping for 30 seconds" << '\n';
    // outfile.flush();
    // std::this_thread::sleep_for(std::chrono::seconds(30));
    outfile << "starting a.exec()" << '\n';
    outfile.flush();
    return a.exec();
  }
  catch(const std::exception& e)
  {
    outfile << e.what() << '\n'; 
    outfile.flush();
    throw;
  }
}
