using Game.Combat;

namespace App.Services
{
    public class Service
    {
        public void Run()
        {
            Game.Combat.Helper.Tick();
        }

        public void Dead() { }
    }
}
